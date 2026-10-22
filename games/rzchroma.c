/* Drop-in RzChromaSDK64.dll / RzChromaSDK.dll that forwards keyboard effects to GK104 Studio.
 *
 * Games load the Razer Chroma SDK DLL and call Init / CreateKeyboardEffect / SetEffect ...
 * This DLL keeps effects locally and sends the active keyboard effect to the Chroma REST
 * emulator in app.py (http://localhost:54235) from a background thread, so the game never
 * waits on HTTP. If GK104 Studio is not running yet, it keeps retrying the session.
 *
 * Build (see build.ps1):
 *   zig cc -target x86_64-windows-gnu -shared -O2 -o RzChromaSDK64.dll rzchroma.c -lwinhttp -lole32
 *   zig cc -target x86-windows-gnu    -shared -O2 -o RzChromaSDK.dll   rzchroma.c -lwinhttp -lole32
 *
 * shortcut: Keyboard CHROMA_CUSTOM2 (8x24 grid) and deprecated keyboard effects return
 * RZRESULT_NOT_SUPPORTED; mouse/headset/mousepad/keypad/ChromaLink effects are accepted and ignored.
 */
#include <windows.h>
#include <winhttp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define EXPORT __declspec(dllexport)
typedef LONG RZRESULT;
typedef GUID RZEFFECTID;
typedef GUID RZDEVICEID;
#define RZRESULT_SUCCESS 0
#define RZRESULT_NOT_FOUND 1168
#define RZRESULT_NOT_SUPPORTED 50
#define RZRESULT_INVALID_PARAMETER 87

#define ROWS 6
#define COLS 22
enum { KB_NONE = 0, KB_BREATHING, KB_CUSTOM, KB_REACTIVE, KB_STATIC, KB_SPECTRUM, KB_WAVE, KB_RESERVED, KB_CUSTOM_KEY };
typedef struct { COLORREF Color[ROWS][COLS]; } CUSTOM_EFFECT;
typedef struct { COLORREF Color[ROWS][COLS]; COLORREF Key[ROWS][COLS]; } CUSTOM_KEY_EFFECT;
typedef struct { COLORREF Color; } STATIC_EFFECT;
typedef struct { int DeviceType; DWORD Connected; } DEVICE_INFO;

#define MAX_EFFECTS 4096
typedef struct { GUID id; char *json; } Effect; /* json == NULL: non-keyboard effect, ignored */

static CRITICAL_SECTION g_lock;
static Effect g_effects[MAX_EFFECTS];
static int g_count;
static char *g_pending;            /* latest keyboard JSON waiting to be sent */
static char g_path[96];            /* "/s/<sid>/chromasdk" once the session exists */
static char g_title[256] = "Game";
static HANDLE g_wake, g_thread;
static volatile LONG g_running;

/* ------------------------------------------------------------------ HTTP (worker thread only) */

/* Returns the HTTP status, or 0 when the app is unreachable or too slow. */
static DWORD http(const wchar_t *method, const char *path, const char *body, char *out, DWORD outlen) {
    DWORD result = 0;
    wchar_t wpath[128];
    HINTERNET s = WinHttpOpen(L"GK104-Chroma", WINHTTP_ACCESS_TYPE_NO_PROXY, NULL, NULL, 0), c = NULL, r = NULL;
    if (!s) return 0;
    WinHttpSetTimeouts(s, 500, 500, 2000, 2000);
    MultiByteToWideChar(CP_UTF8, 0, path, -1, wpath, 128);
    /* 127.0.0.1, not "localhost": the app listens on IPv4 only and ::1 would be tried first */
    if ((c = WinHttpConnect(s, L"127.0.0.1", 54235, 0)) &&
        (r = WinHttpOpenRequest(c, method, wpath, NULL, NULL, NULL, 0))) {
        DWORD len = body ? (DWORD)strlen(body) : 0;
        if (WinHttpSendRequest(r, L"Content-Type: application/json\r\n", (DWORD)-1, (void *)body, len, len, 0) &&
            WinHttpReceiveResponse(r, NULL)) {
            DWORD status = 0, sz = sizeof status, got = 0, n;
            WinHttpQueryHeaders(r, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, NULL, &status, &sz, NULL);
            if (out) {
                while (got + 1 < outlen && WinHttpReadData(r, out + got, outlen - 1 - got, &n) && n) got += n;
                out[got] = 0;
            }
            result = status;
        }
    }
    if (r) WinHttpCloseHandle(r);
    if (c) WinHttpCloseHandle(c);
    WinHttpCloseHandle(s);
    return result;
}

static void open_session(void) {
    char body[512], resp[512], *p, *q;
    snprintf(body, sizeof body,
             "{\"title\":\"%s\",\"description\":\"game\",\"author\":{\"name\":\"game\",\"contact\":\"none\"},"
             "\"device_supported\":[\"keyboard\"],\"category\":\"game\"}", g_title);
    if (http(L"POST", "/razer/chromasdk", body, resp, sizeof resp) != 200) return;
    /* response: {"sessionid": N, "uri": "http://localhost:54235/s/N/chromasdk"} */
    if ((p = strstr(resp, "/s/")) && (q = strchr(p, '"')) && q - p < (int)sizeof g_path) {
        EnterCriticalSection(&g_lock);
        memcpy(g_path, p, q - p);
        g_path[q - p] = 0;
        LeaveCriticalSection(&g_lock);
    }
}

static DWORD WINAPI worker(void *arg) {
    char url[128];
    (void)arg;
    while (g_running) {
        WaitForSingleObject(g_wake, 1000);
        if (!g_running) break;
        if (!g_path[0]) { open_session(); if (!g_path[0]) continue; }
        EnterCriticalSection(&g_lock);
        char *json = g_pending;
        g_pending = NULL;
        LeaveCriticalSection(&g_lock);
        DWORD st;
        if (json) {
            snprintf(url, sizeof url, "%s/keyboard", g_path);
            st = http(L"PUT", url, json, NULL, 0);
            EnterCriticalSection(&g_lock);   /* not delivered: keep it to resend unless a newer frame came */
            if (st != 200 && !g_pending) g_pending = json; else free(json);
            LeaveCriticalSection(&g_lock);
            if (st != 200 && st != 404) SetEvent(g_wake);  /* resend soon */
        } else {
            snprintf(url, sizeof url, "%s/heartbeat", g_path);
            st = http(L"PUT", url, "{}", NULL, 0);
        }
        if (st == 404) g_path[0] = 0;  /* app restarted, session gone: open a new one next loop */
        else if (st != 200) Sleep(200);  /* app slow or closed: retry without spinning */
    }
    return 0;
}

/* ------------------------------------------------------------------ effects */

static char *grid_json(char *p, const COLORREF g[ROWS][COLS]) {
    *p++ = '[';
    for (int r = 0; r < ROWS; r++) {
        *p++ = '[';
        for (int c = 0; c < COLS; c++) p += sprintf(p, c ? ",%lu" : "%lu", (unsigned long)g[r][c]);
        *p++ = ']';
        if (r < ROWS - 1) *p++ = ',';
    }
    *p++ = ']';
    *p = 0;
    return p;
}

static char *keyboard_json(int type, const void *param) {
    char *buf = malloc(8192), *p = buf;
    if (!buf) return NULL;
    switch (type) {
    case KB_NONE:
        strcpy(buf, "{\"effect\":\"CHROMA_NONE\"}");
        return buf;
    case KB_STATIC:
        if (!param) break;
        sprintf(buf, "{\"effect\":\"CHROMA_STATIC\",\"param\":{\"color\":%lu}}", (unsigned long)((const STATIC_EFFECT *)param)->Color);
        return buf;
    case KB_CUSTOM:
        if (!param) break;
        p += sprintf(p, "{\"effect\":\"CHROMA_CUSTOM\",\"param\":");
        p = grid_json(p, ((const CUSTOM_EFFECT *)param)->Color);
        strcpy(p, "}");
        return buf;
    case KB_CUSTOM_KEY:
        if (!param) break;
        p += sprintf(p, "{\"effect\":\"CHROMA_CUSTOM_KEY\",\"param\":{\"color\":");
        p = grid_json(p, ((const CUSTOM_KEY_EFFECT *)param)->Color);
        p += sprintf(p, ",\"key\":");
        p = grid_json(p, ((const CUSTOM_KEY_EFFECT *)param)->Key);
        strcpy(p, "}}");
        return buf;
    }
    free(buf);
    return NULL;
}

static void queue_json(char *json) {  /* takes ownership */
    EnterCriticalSection(&g_lock);
    free(g_pending);
    g_pending = json;
    LeaveCriticalSection(&g_lock);
    SetEvent(g_wake);
}

static RZRESULT store(char *json, RZEFFECTID *id) {
    if (!id) {  /* SDK rule: no id pointer means "apply now" */
        if (json) queue_json(json);
        return RZRESULT_SUCCESS;
    }
    EnterCriticalSection(&g_lock);
    if (g_count >= MAX_EFFECTS) { LeaveCriticalSection(&g_lock); free(json); return RZRESULT_INVALID_PARAMETER; }
    CoCreateGuid(id);
    g_effects[g_count].id = *id;
    g_effects[g_count].json = json;
    g_count++;
    LeaveCriticalSection(&g_lock);
    return RZRESULT_SUCCESS;
}

static int find(RZEFFECTID id) {
    for (int i = 0; i < g_count; i++)
        if (IsEqualGUID(&g_effects[i].id, &id)) return i;
    return -1;
}

/* ------------------------------------------------------------------ exports */

static RZRESULT start(void) {
    if (InterlockedExchange(&g_running, 1)) return RZRESULT_SUCCESS;
    g_wake = CreateEventW(NULL, FALSE, TRUE, NULL);
    g_thread = CreateThread(NULL, 0, worker, NULL, 0, NULL);
    return RZRESULT_SUCCESS;
}

static void set_title(const wchar_t *w) {  /* UTF-8, without characters that would break the JSON */
    WideCharToMultiByte(CP_UTF8, 0, w, -1, g_title, sizeof g_title - 1, NULL, NULL);
    g_title[sizeof g_title - 1] = 0;
    for (char *c = g_title; *c; c++) if (*c == '"' || *c == '\\' || (unsigned char)*c < 32) *c = ' ';
}

static void exe_title(void) {
    wchar_t exe[MAX_PATH], *name;
    GetModuleFileNameW(NULL, exe, MAX_PATH);
    name = wcsrchr(exe, L'\\');
    set_title(name ? name + 1 : exe);
}

EXPORT RZRESULT Init(void) {
    exe_title();
    return start();
}

EXPORT RZRESULT InitSDK(const void *appinfo) {  /* APPINFOTYPE starts with wchar_t Title[256] */
    exe_title();
    if (appinfo && *(const wchar_t *)appinfo) {
        wchar_t t[256];
        wcsncpy(t, (const wchar_t *)appinfo, 255);
        t[255] = 0;
        set_title(t);
    }
    return start();
}

EXPORT RZRESULT UnInit(void) {
    if (!InterlockedExchange(&g_running, 0)) return RZRESULT_SUCCESS;
    SetEvent(g_wake);
    WaitForSingleObject(g_thread, 2000);
    CloseHandle(g_thread);
    CloseHandle(g_wake);
    if (g_path[0]) http(L"DELETE", g_path, NULL, NULL, 0);
    EnterCriticalSection(&g_lock);
    for (int i = 0; i < g_count; i++) free(g_effects[i].json);
    g_count = 0;
    free(g_pending);
    g_pending = NULL;
    g_path[0] = 0;
    LeaveCriticalSection(&g_lock);
    return RZRESULT_SUCCESS;
}

EXPORT RZRESULT CreateKeyboardEffect(int type, const void *param, RZEFFECTID *id) {
    char *json = keyboard_json(type, param);
    if (!json) return RZRESULT_NOT_SUPPORTED;
    return store(json, id);
}

/* Other devices: accepted so games keep running, but there is nothing to light. */
EXPORT RZRESULT CreateEffect(RZDEVICEID dev, int type, const void *param, RZEFFECTID *id) { (void)dev; (void)type; (void)param; return store(NULL, id); }
EXPORT RZRESULT CreateMouseEffect(int type, const void *param, RZEFFECTID *id) { (void)type; (void)param; return store(NULL, id); }
EXPORT RZRESULT CreateHeadsetEffect(int type, const void *param, RZEFFECTID *id) { (void)type; (void)param; return store(NULL, id); }
EXPORT RZRESULT CreateMousepadEffect(int type, const void *param, RZEFFECTID *id) { (void)type; (void)param; return store(NULL, id); }
EXPORT RZRESULT CreateKeypadEffect(int type, const void *param, RZEFFECTID *id) { (void)type; (void)param; return store(NULL, id); }
EXPORT RZRESULT CreateChromaLinkEffect(int type, const void *param, RZEFFECTID *id) { (void)type; (void)param; return store(NULL, id); }

EXPORT RZRESULT SetEffect(RZEFFECTID id) {
    char *json = NULL;
    EnterCriticalSection(&g_lock);
    int i = find(id);
    if (i >= 0 && g_effects[i].json) json = _strdup(g_effects[i].json);
    LeaveCriticalSection(&g_lock);
    if (i < 0) return RZRESULT_NOT_FOUND;
    if (json) queue_json(json);
    return RZRESULT_SUCCESS;
}

EXPORT RZRESULT DeleteEffect(RZEFFECTID id) {
    EnterCriticalSection(&g_lock);
    int i = find(id);
    if (i >= 0) {
        free(g_effects[i].json);
        g_effects[i] = g_effects[--g_count];
    }
    LeaveCriticalSection(&g_lock);
    return i >= 0 ? RZRESULT_SUCCESS : RZRESULT_NOT_FOUND;
}

EXPORT RZRESULT RegisterEventNotification(HWND hwnd) { (void)hwnd; return RZRESULT_SUCCESS; }
EXPORT RZRESULT UnregisterEventNotification(void) { return RZRESULT_SUCCESS; }

EXPORT RZRESULT QueryDevice(RZDEVICEID dev, DEVICE_INFO *info) {  /* report every device as a connected keyboard */
    (void)dev;
    if (!info) return RZRESULT_INVALID_PARAMETER;
    info->DeviceType = 1;
    info->Connected = 1;
    return RZRESULT_SUCCESS;
}

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, void *r) {
    (void)h; (void)r;
    if (reason == DLL_PROCESS_ATTACH) InitializeCriticalSection(&g_lock);
    return TRUE;
}
