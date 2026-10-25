# LLBot 试验：只读预检备份脚本
# 目的：在任何真实账号测试前，备份可能被触碰的生产配置，并建立完整性基线。
# 纪律：只复制/只读，不修改任何生产文件；不执行 git；不记录任何密钥内容。
# 运行：pwsh -NoProfile -File .\preflight_backup.ps1

$ErrorActionPreference = 'Stop'
$Repo = 'E:\QQbot'
$Findings = Join-Path $Repo 'qq_native_re\findings\llbot_trial'
$Ts = Get-Date -Format 'yyyyMMdd-HHmmss'
$Dest = Join-Path $Repo "qq_native_re\patches\llbot_trial_$Ts"
$NapCatCfg = Join-Path $Repo 'NapCat\NapCat.44498.Shell\versions\9.9.26-44498\resources\app\napcat\config'

New-Item -ItemType Directory -Force -Path $Dest | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Dest 'napcat_config') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Dest 'config') | Out-Null

$backedUpAt = (Get-Date).ToString('o')
$entries = New-Object System.Collections.Generic.List[object]

function Get-Sha256Lenient {
    # 日志可能被运行中的 Python 以独占方式占用；用 ReadWrite 共享模式打开后流式计算，
    # 仍失败则返回 'LOCKED'（不阻断备份，但会在 manifest 里显式标注）。
    param([string]$Path)
    try {
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        try {
            $sha = [System.Security.Cryptography.SHA256]::Create()
            try { return ([System.BitConverter]::ToString($sha.ComputeHash($fs)) -replace '-', '') }
            finally { $sha.Dispose() }
        } finally { $fs.Dispose() }
    } catch { return 'LOCKED' }
}

function Add-BackupEntry {
    param([string]$Src, [string]$Dst, [bool]$Sensitive, [string]$Note)
    $hash = (Get-FileHash -Path $Src -Algorithm SHA256).Hash
    $fi = Get-Item -Path $Src
    $entries.Add([ordered]@{
        original_path = $Src
        backup_path   = $Dst
        sha256        = $hash
        size_bytes    = [int64]$fi.Length
        last_write    = $fi.LastWriteTime.ToString('o')
        backed_up_at  = $backedUpAt
        sensitive     = $Sensitive
        note          = $Note
    })
}

# 1) 生产 config/ 全目录（75 KB，均为 JSON/markdown，不含 API key）
Get-ChildItem -Path (Join-Path $Repo 'config') -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Substring((Join-Path $Repo 'config').Length).TrimStart('\')
    $dst = Join-Path (Join-Path $Dest 'config') $rel
    New-Item -ItemType Directory -Force -Path (Split-Path $dst -Parent) | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $dst -Force
    Add-BackupEntry -Src $_.FullName -Dst $dst -Sensitive $false -Note 'production config/'
}

# 2) NapCat 运行配置全目录（含 webui token / onebot token / passkey，标 sensitive）
Get-ChildItem -Path $NapCatCfg -File | ForEach-Object {
    $dst = Join-Path (Join-Path $Dest 'napcat_config') $_.Name
    Copy-Item -LiteralPath $_.FullName -Destination $dst -Force
    Add-BackupEntry -Src $_.FullName -Dst $dst -Sensitive $true -Note 'NapCat runtime config (contains tokens/passkey; never quote values)'
}

# 3) 生产私聊永久数据：只做完整性快照，不复制（避免私聊内容二次落盘）
$integrity = New-Object System.Collections.Generic.List[object]
$integrityRoots = @(
    (Join-Path $Repo 'data\private'),
    (Join-Path $Repo 'data\roster.json'),
    (Join-Path $Repo 'user_profiles.json'),
    (Join-Path $Repo 'menu_data.json'),
    (Join-Path $Repo 'scripts\sign_in_data.json'),
    (Join-Path $Repo 'logs')
)
foreach ($root in $integrityRoots) {
    if (-not (Test-Path $root)) { continue }
    $files = if ((Get-Item $root).PSIsContainer) { Get-ChildItem -Path $root -Recurse -File } else { Get-Item $root }
    foreach ($f in $files) {
        $integrity.Add([ordered]@{
            path       = $f.FullName
            sha256     = (Get-Sha256Lenient -Path $f.FullName)
            size_bytes = [int64]$f.Length
            last_write = $f.LastWriteTime.ToString('o')
        })
    }
}

# 4) NapCat 关键可执行文件指纹（回滚时用于验证未被改动）
$napcatHashes = @()
foreach ($p in @(
    (Join-Path $Repo 'NapCat\NapCat.44498.Shell\NapCatWinBootMain.exe'),
    (Join-Path $Repo 'NapCat\NapCat.44498.Shell\NapCatWinBootHook.dll'),
    (Join-Path $Repo 'NapCat\NapCat.44498.Shell\QQ.exe'),
    (Join-Path $Repo 'NapCat\NapCat.44498.Shell\versions\9.9.26-44498\resources\app\napcat\napcat.mjs'),
    (Join-Path $Repo 'tools\start_napcat_verbose.bat')
)) {
    if (Test-Path $p) {
        $napcatHashes += [ordered]@{
            path       = $p
            sha256     = (Get-FileHash -Path $p -Algorithm SHA256).Hash
            size_bytes = [int64](Get-Item $p).Length
        }
    }
}

$manifest = [ordered]@{
    trial            = 'llbot_trial'
    generated_at     = $backedUpAt
    backup_dir       = $Dest
    backup_entries   = $entries
    integrity_snapshot = $integrity
    napcat_fingerprints = $napcatHashes
    notes            = @(
        'backup_entries 为实际复制的文件；integrity_snapshot 只记录哈希，不复制私聊内容。',
        'sensitive=true 的备份含本地 token/passkey，仅存本机，不得写入报告或外发。',
        '.env 未备份：本轮不修改，且避免复制 API key。'
    )
}

$out = Join-Path $Findings 'manifest.json'
$manifest | ConvertTo-Json -Depth 8 | Set-Content -Path $out -Encoding utf8
Copy-Item -LiteralPath $out -Destination (Join-Path $Dest 'manifest.json') -Force

Write-Output "BACKUP_DIR=$Dest"
Write-Output "MANIFEST=$out"
Write-Output ("FILES_BACKED_UP=" + $entries.Count)
Write-Output ("INTEGRITY_FILES=" + $integrity.Count)

