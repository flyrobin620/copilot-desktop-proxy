# Compatibility shim: Claude App configurations written by releases up to 1.1.19
# call this path. Install.cmd re-points them to scripts\; this file can be
# removed once no installation still uses the old path. Prints only what the
# real helper prints (the local placeholder).
& (Join-Path $PSScriptRoot 'scripts\claude-gateway-auth.ps1')
exit $LASTEXITCODE
