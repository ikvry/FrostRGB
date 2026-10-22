# Builds the Chroma SDK replacement DLLs (needs zig: scoop install zig).
Set-Location $PSScriptRoot
zig cc -target x86_64-windows-gnu -shared -O2 -o RzChromaSDK64.dll rzchroma.c -lwinhttp -lole32
if ($LASTEXITCODE) { exit $LASTEXITCODE }
zig cc -target x86-windows-gnu -shared -O2 -o RzChromaSDK.dll rzchroma.c -lwinhttp -lole32
if ($LASTEXITCODE) { exit $LASTEXITCODE }
Remove-Item *.lib, *.pdb -ErrorAction SilentlyContinue
Get-Item RzChromaSDK64.dll, RzChromaSDK.dll | Select-Object Name, Length
