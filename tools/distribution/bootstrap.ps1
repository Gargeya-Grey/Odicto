#Requires -Version 5.1
param([string]$HomePath)
$ErrorActionPreference = 'Stop'
$uvCommand = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
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
import hashlib, io, json, stat, sys, urllib.error, urllib.parse, urllib.request, zipfile
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
def get(url, maximum, soft=False):
    if not trusted(url): raise SystemExit('Untrusted download URL')
    request = urllib.request.Request(url, headers={'User-Agent': 'Odicto-bootstrap/1'})
    try:
        with urllib.request.build_opener(Redirect()).open(request, timeout=30) as response:
            data = response.read(maximum + 1)
    except urllib.error.HTTPError as error:
        if soft: return None
        if error.code == 429 or (error.code == 403 and (error.headers or {}).get('X-RateLimit-Remaining') == '0'):
            raise SystemExit('GitHub refused the download for now: too many requests from this network. Wait one hour, then run the installer again. Odicto was not installed.')
        raise SystemExit(f'The download failed: GitHub answered HTTP {error.code}. Run the installer again later. Odicto was not installed.')
    except (urllib.error.URLError, OSError) as error:
        raise SystemExit(f'The download failed: no connection to GitHub ({getattr(error, "reason", error)}). Check the internet connection, then run the installer again. Odicto was not installed.')
    if len(data) > maximum:
        if soft: return None
        raise SystemExit('Download exceeds size limit')
    return data
import re
def pick(data):
    try: metadata = json.loads(data)
    except ValueError: metadata = None
    tag = metadata.get('tag_name') if isinstance(metadata, dict) else None
    if not isinstance(tag, str) or not re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag) or metadata.get('draft') or metadata.get('prerelease'):
        return None, 'A stable versioned release is required'
    assets = metadata.get('assets') if isinstance(metadata.get('assets'), list) else []
    for asset in (f'Odicto-{tag}.zip', f'Odicto-{tag}.zip.sha256'):
        matches = [a for a in assets if isinstance(a, dict) and a.get('name') == asset]
        if len(matches) != 1 or matches[0].get('browser_download_url') != f'https://github.com/{repo}/releases/download/{tag}/' + asset:
            return None, 'Release archive or checksum is missing'
    return tag, ''
# The release manifest has no hourly limit; the API answers only when the manifest gives an HTTP error or is invalid.
data = get(f'https://github.com/{repo}/releases/latest/download/odicto-release.json', 2*1024*1024, True)
tag, problem = pick(data) if data is not None else (None, '')
if tag is None:
    tag, problem = pick(get(f'https://api.github.com/repos/{repo}/releases/latest', 2*1024*1024))
    if tag is None: raise SystemExit(problem)
name = f'Odicto-{tag}.zip'
prefix = f'https://github.com/{repo}/releases/download/{tag}/'
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
$env:PATH = (Split-Path -Parent $uv) + [IO.Path]::PathSeparator + $env:PATH
$bootstrap | & $uv run --python 3.12 --no-project --no-config python - @managerArgs
if ($LASTEXITCODE -ne 0) { throw 'Odicto managed installation failed' }
