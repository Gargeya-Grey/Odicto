#Requires -Version 5.1
param([string]$HomePath)
$ErrorActionPreference = 'Stop'
$uvCommand = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue
if ($uvCommand) {
    $uv = $uvCommand.Source
} else {
    $installer = Join-Path ([IO.Path]::GetTempPath()) ('odicto-uv-' + [guid]::NewGuid().ToString('N') + '.ps1')
    try {
        Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/install.ps1' -OutFile $installer
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer
        if ($LASTEXITCODE -ne 0) { throw 'uv installation failed' }
    } finally {
        Remove-Item -LiteralPath $installer -ErrorAction SilentlyContinue
    }
    $uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (-not (Test-Path -LiteralPath $uv)) { $uv = Join-Path $env:USERPROFILE '.cargo\bin\uv.exe' }
}
$bootstrap = @'
import hashlib, io, json, stat, sys, urllib.parse, urllib.request, zipfile
repo = 'Gargeya-Grey/Odicto'
hosts = {'api.github.com', 'github.com', 'release-assets.githubusercontent.com',
         'objects.githubusercontent.com', 'github-releases.githubusercontent.com'}
def trusted(url):
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme == 'https' and parsed.hostname in hosts and parsed.port in (None, 443) and not parsed.username and not parsed.password
class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, url):
        if not trusted(url): raise SystemExit('Untrusted download redirect')
        return super().redirect_request(req, fp, code, msg, headers, url)
def get(url, maximum):
    if not trusted(url): raise SystemExit('Untrusted download URL')
    request = urllib.request.Request(url, headers={'User-Agent': 'Odicto-bootstrap/1'})
    with urllib.request.build_opener(Redirect()).open(request, timeout=30) as response:
        data = response.read(maximum + 1)
    if len(data) > maximum: raise SystemExit('Download exceeds size limit')
    return data
metadata = json.loads(get(f'https://api.github.com/repos/{repo}/releases/latest', 2*1024*1024))
tag = metadata.get('tag_name', '')
import re
if not re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag) or metadata.get('draft') or metadata.get('prerelease'):
    raise SystemExit('A stable versioned release is required')
name = f'Odicto-{tag}.zip'
prefix = f'https://github.com/{repo}/releases/download/{tag}/'
for asset in (name, name + '.sha256'):
    matches = [a for a in metadata.get('assets', []) if a.get('name') == asset]
    if len(matches) != 1 or matches[0].get('browser_download_url') != prefix + asset:
        raise SystemExit('Release archive or checksum is missing')
checksum = get(prefix + name + '.sha256', 1024).decode('ascii').strip().split()
if len(checksum) != 2 or not re.fullmatch('[0-9a-fA-F]{64}', checksum[0]) or checksum[1] != name:
    raise SystemExit('Invalid release checksum')
archive = get(prefix + name, 128*1024*1024)
if hashlib.sha256(archive).hexdigest() != checksum[0].lower(): raise SystemExit('Release checksum mismatch')
with zipfile.ZipFile(io.BytesIO(archive)) as release:
    matches = [i for i in release.infolist() if i.filename == 'Odicto/tools/distribution/manager.py']
    if len(matches) != 1 or matches[0].file_size > 512*1024 or stat.S_ISLNK(matches[0].external_attr >> 16):
        raise SystemExit('This release does not include the managed installer; use the release installation instructions')
    source = release.read(matches[0])
import types
module = types.ModuleType('odicto_verified_bootstrap')
sys.modules[module.__name__] = module
namespace = module.__dict__
namespace['__file__'] = '<verified-release-manager>'
exec(compile(source, '<verified-release-manager>', 'exec'), namespace)
raise SystemExit(namespace['main']([*sys.argv[1:], 'install']))
'@
$managerArgs = @()
if ($HomePath) { $managerArgs = @('--home', $HomePath) }
$bootstrap | & $uv run --python 3.12 --no-project --no-config python - @managerArgs
if ($LASTEXITCODE -ne 0) { throw 'Odicto managed installation failed' }
