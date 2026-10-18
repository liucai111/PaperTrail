[CmdletBinding()]
param(
    [ValidateSet('start','status','stop','restart','doctor')]
    [string]$Action = 'start',
    [ValidateSet('dev','preview')]
    [string]$Mode = '',
    [int]$ApiPort = 0,
    [int]$WebPort = 0,
    [string]$PythonExe = '',
    [int]$HealthTimeoutSeconds = 0
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$configPath = Join-Path $PSScriptRoot 'papertrail.config.json'
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$apiDir = Join-Path $repo 'apps\api'
$webDir = Join-Path $repo 'apps\web'
$runtimeDir = Join-Path $repo 'var\runtime'
$runtimeFile = Join-Path $runtimeDir 'papertrail.json'
$logDir = Join-Path $repo 'var\logs'
if (-not $ApiPort) { $ApiPort = [int]$config.apiPort }
if (-not $WebPort) { $WebPort = [int]$config.webPort }
if (-not $Mode) { $Mode = [string]$config.mode }
if (-not $HealthTimeoutSeconds) { $HealthTimeoutSeconds = [int]$config.healthTimeoutSeconds }

function Get-ResolvedPython {
    if ($PythonExe) {
        if (-not (Test-Path -LiteralPath $PythonExe)) { throw "Python 不存在：$PythonExe" }
        return (Resolve-Path -LiteralPath $PythonExe).Path
    }
    $venvPython = Join-Path $repo '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython) { return $venvPython }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if (-not $launcher) { throw '未找到 .venv Python 或 py.exe；运行 README 中的环境准备命令。' }
    $resolved = (& $launcher.Source "-$($config.pythonVersion)" -c 'import sys; print(sys.executable)').Trim()
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $resolved)) { throw "Python $($config.pythonVersion) 启动器检查失败。" }
    return $resolved
}

function Get-NodeCommand {
    $node = Get-Command node.exe -ErrorAction SilentlyContinue
    if (-not $node) { throw '未找到 node.exe。请安装 Node.js 并运行 npm.cmd ci。' }
    return $node.Source
}

function Get-ListenerInfo([int]$Port) {
    $connections = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
    $rows = @()
    foreach ($connection in $connections) {
        $rows += [pscustomobject]@{LocalAddress=$connection.LocalAddress;OwningProcess=[int]$connection.OwningProcess}
    }
    if (-not $rows) {
        $netstat = @(netstat -ano -p tcp 2>$null)
        foreach ($line in $netstat) {
            if ($line -match '^\s*TCP\s+(?<local>\S+):(?<port>\d+)\s+\S+\s+LISTENING\s+(?<pid>\d+)\s*$' -and [int]$Matches.port -eq $Port) {
                $rows += [pscustomobject]@{LocalAddress=$Matches.local;OwningProcess=[int]$Matches.pid}
            }
        }
    }
    if (-not $rows) { return @() }
    $seen = @{}
    $items = @()
    foreach ($connection in $rows) {
        $pidValue = [int]$connection.OwningProcess
        if ($seen.ContainsKey($pidValue)) { continue }
        $seen[$pidValue] = $true
        $proc = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $pidValue" -ErrorAction SilentlyContinue
        $path = $null
        $started = $null
        if ($proc) {
            try { $path = $proc.Path } catch { $path = $null }
            try { $started = $proc.StartTime.ToUniversalTime().ToString('o') } catch { $started = $null }
        }
        $items += [pscustomobject]@{
            Port = $Port; Address = $connection.LocalAddress; Pid = $pidValue
            ProcessName = if ($proc) { $proc.ProcessName } else { 'unknown' }
            Executable = $path; StartedUtc = $started; CommandLine = if ($cim) { $cim.CommandLine } else { $null }
        }
    }
    return $items
}

function Get-ProcessIdentity($Entry,[int]$ExpectedPort=0,[switch]$AllowLegacyManifest) {
    $proc = Get-Process -Id ([int]$Entry.pid) -ErrorAction SilentlyContinue
    if (-not $proc) { return [pscustomobject]@{Valid=$false;Reason='process-exited';Process=$null} }
    try {
        $actualPath = [IO.Path]::GetFullPath([string]$proc.Path)
        $actualTicks = $proc.StartTime.ToUniversalTime().Ticks
    } catch {
        return [pscustomobject]@{Valid=$false;Reason='process-identity-unreadable';Process=$proc}
    }
    if ($actualTicks -ne [long]$Entry.startUtcTicks -or
        -not [string]::Equals($actualPath, [IO.Path]::GetFullPath([string]$Entry.executable), [StringComparison]::OrdinalIgnoreCase)) {
        return [pscustomobject]@{Valid=$false;Reason='pid-reused-or-path-changed';Process=$proc}
    }
    $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $($Entry.pid)" -ErrorAction SilentlyContinue
    if (-not $cim -or -not $cim.CommandLine) {
        if ($AllowLegacyManifest -and $ExpectedPort -gt 0) {
            $listenerMatch = @(Get-ListenerInfo $ExpectedPort | Where-Object { $_.Pid -eq [int]$Entry.pid })
            if ($listenerMatch.Count -gt 0) { return [pscustomobject]@{Valid=$true;Reason='legacy-manifest-path-start-and-listener-matched';Process=$proc;CommandLine=$null} }
        }
        return [pscustomobject]@{Valid=$false;Reason='command-line-unreadable';Process=$proc}
    }
    if ($Entry.commandLine) {
        $expectedCommand = (([string]$Entry.commandLine).Replace('"','').Replace("'",'').ToLowerInvariant() -replace '\s+',' ').Trim()
        $actualCommand = (([string]$cim.CommandLine).Replace('"','').Replace("'",'').ToLowerInvariant() -replace '\s+',' ').Trim()
        if ($actualCommand -ne $expectedCommand) {
            return [pscustomobject]@{Valid=$false;Reason='command-line-changed';Process=$proc}
        }
    } else {
        if ($AllowLegacyManifest -and $ExpectedPort -gt 0) {
            $listenerMatch = @(Get-ListenerInfo $ExpectedPort | Where-Object { $_.Pid -eq [int]$Entry.pid })
            if ($listenerMatch.Count -gt 0) { return [pscustomobject]@{Valid=$true;Reason='legacy-manifest-path-start-and-listener-matched';Process=$proc;CommandLine=$cim.CommandLine} }
        } else {
            $repoNeedle = $repo.ToLowerInvariant()
            $command = ([string]$cim.CommandLine).ToLowerInvariant()
            $roleNeedle = if ($Entry.name -eq 'api') { 'uvicorn app.main:app' } else { 'vite' }
            if (-not $command.Contains($repoNeedle) -or -not $command.Contains($roleNeedle)) {
                return [pscustomobject]@{Valid=$false;Reason='legacy-command-not-project-service';Process=$proc}
            }
        }
    }
    return [pscustomobject]@{Valid=$true;Reason='identity-matched';Process=$proc;CommandLine=$cim.CommandLine}
}

function Stop-VerifiedEntry($Entry,[switch]$AllowLegacyManifest,[int]$ExpectedPort=0) {
    $identity = Get-ProcessIdentity $Entry $ExpectedPort -AllowLegacyManifest:$AllowLegacyManifest
    if (-not $identity.Valid) { return $identity.Reason }
    try {
        Stop-Process -Id $identity.Process.Id -ErrorAction Stop
        $deadline = (Get-Date).AddSeconds(5)
        do {
            $remaining = Get-Process -Id $identity.Process.Id -ErrorAction SilentlyContinue
            if (-not $remaining) { return 'stopped-owned-process' }
            try { if ($remaining.StartTime.ToUniversalTime().Ticks -ne [long]$Entry.startUtcTicks) { return 'stopped-owned-process' } } catch { return 'stopped-owned-process' }
            Start-Sleep -Milliseconds 100
        } while ((Get-Date) -lt $deadline)
        return 'stop-requested-process-still-exiting'
    } catch {
        return "stop-denied: $($_.Exception.GetType().Name)"
    }
}

function Stop-JustCreatedProcess($Entry) {
    $proc = Get-Process -Id ([int]$Entry.pid) -ErrorAction SilentlyContinue
    if (-not $proc) { return 'already-exited' }
    try {
        $sameStart = $proc.StartTime.ToUniversalTime().Ticks -eq [long]$Entry.startUtcTicks
        $samePath = [string]::Equals([IO.Path]::GetFullPath([string]$proc.Path), [IO.Path]::GetFullPath([string]$Entry.executable), [StringComparison]::OrdinalIgnoreCase)
        if (-not $sameStart -or -not $samePath) { return 'identity-mismatch-left-running' }
        Stop-Process -Id $proc.Id -ErrorAction Stop
        return 'stopped-this-launch-process'
    } catch { return "cleanup-denied: $($_.Exception.GetType().Name)" }
}

function Get-ApiVersion([int]$Port) {
    try { return Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/v1/version" -TimeoutSec 3 }
    catch { return $null }
}

function Get-ApiHealth([int]$Port) {
    try { return Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/v1/health" -TimeoutSec 3 }
    catch { return $null }
}

function Get-WebInfo([int]$Port) {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -TimeoutSec 3 -UseBasicParsing
        $build = ''
        if ($response.Content -match 'name="papertrail-build" content="([^"]+)"') { $build = $Matches[1] }
        $proxied = $null
        try { $proxied = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/v1/version" -TimeoutSec 3 } catch { }
        return [pscustomobject]@{StatusCode=$response.StatusCode;BuildId=$build;ProxiedApi=$proxied}
    } catch { return $null }
}

function Test-SourceNewerThanBuild {
    $distIndex = Join-Path $webDir 'dist\index.html'
    if (-not (Test-Path -LiteralPath $distIndex)) { return $true }
    $sourceRoots = @((Join-Path $webDir 'src'), (Join-Path $webDir 'index.html'),
        (Join-Path $webDir 'vite.config.ts'), (Join-Path $webDir 'package.json'),
        (Join-Path $webDir 'package-lock.json'), (Join-Path $webDir 'tsconfig.json'))
    $newest = [DateTime]::MinValue
    foreach ($source in $sourceRoots) {
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            $time = (Get-Item -LiteralPath $source).LastWriteTimeUtc
            if ($time -gt $newest) { $newest = $time }
        } elseif (Test-Path -LiteralPath $source -PathType Container) {
            $file = Get-ChildItem -LiteralPath $source -File -Recurse -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -notmatch '\\(node_modules|dist|\.vite)\\' } |
                Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
            if ($file -and $file.LastWriteTimeUtc -gt $newest) { $newest = $file.LastWriteTimeUtc }
        }
    }
    return $newest -gt (Get-Item -LiteralPath $distIndex).LastWriteTimeUtc
}

function Get-SafeProxyHosts {
    $values = @()
    foreach ($value in @($env:NO_PROXY, $env:no_proxy)) {
        if ($value) { $values += $value.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ } }
    }
    $values += @($config.noProxyHosts)
    return @($values | Select-Object -Unique)
}

function Get-ExecutionPolicySummary {
    $processPolicy = [Environment]::GetEnvironmentVariable('PSExecutionPolicyPreference','Process')
    if (-not $processPolicy) { $processPolicy = 'not-overridden' }
    $machinePolicy = 'not-defined'
    $userPolicy = 'not-defined'
    try {
        $value = (Get-ItemProperty -LiteralPath 'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Microsoft\Windows\PowerShell' -Name ExecutionPolicy -ErrorAction Stop).ExecutionPolicy
        if ($value) { $machinePolicy = [string]$value }
    } catch { }
    try {
        $value = (Get-ItemProperty -LiteralPath 'Registry::HKEY_CURRENT_USER\SOFTWARE\Policies\Microsoft\Windows\PowerShell' -Name ExecutionPolicy -ErrorAction Stop).ExecutionPolicy
        if ($value) { $userPolicy = [string]$value }
    } catch { }
    return [pscustomobject]@{Process=$processPolicy;MachinePolicy=$machinePolicy;UserPolicy=$userPolicy}
}

function Get-PathAliasCount {
    $keys = @([Environment]::GetEnvironmentVariables('Process').Keys | Where-Object { [string]::Equals([string]$_,'Path',[StringComparison]::OrdinalIgnoreCase) })
    return $keys.Count
}

function Normalize-LauncherProcessPath {
    $environment = [Environment]::GetEnvironmentVariables('Process')
    $pathNames = @($environment.Keys | Where-Object { [string]::Equals([string]$_,'Path',[StringComparison]::OrdinalIgnoreCase) })
    if ($pathNames.Count -le 1) { return }
    $pathValues = @()
    foreach ($name in $pathNames) {
        $value = [string]$environment[$name]
        if ($value -and $pathValues -notcontains $value) { $pathValues += $value }
    }
    foreach ($name in $pathNames) { [Environment]::SetEnvironmentVariable([string]$name,$null,'Process') }
    [Environment]::SetEnvironmentVariable('PATH',($pathValues -join ';'),'Process')
    $remaining = @([Environment]::GetEnvironmentVariables('Process').Keys | Where-Object { [string]::Equals([string]$_,'Path',[StringComparison]::OrdinalIgnoreCase) })
    if ($remaining.Count -gt 1) { throw '当前 PowerShell 进程仍有大小写重复的 PATH 环境键，无法安全创建服务子进程。请在普通 PowerShell 启动。' }
}

function Test-RequiredProxyBypass([string]$HostName, [string[]]$Entries) {
    foreach ($entry in $Entries) {
        $candidate = $entry.Trim().TrimStart('.')
        if ($candidate -eq '*' -or $HostName -eq $candidate -or $HostName.EndsWith('.' + $candidate, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}

function Test-PortFree([int]$Port) {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback,$Port)
    try { $listener.Start(); return $true }
    catch { return $false }
    finally { $listener.Stop() }
}

function Get-ManagedStateFiles {
    if (-not (Test-Path -LiteralPath $runtimeDir)) { return @() }
    return @(Get-ChildItem -LiteralPath $runtimeDir -Filter 'papertrail*.json' -File -ErrorAction SilentlyContinue)
}

function Show-Status {
    Write-Output "PaperTrail root: $repo"
    Write-Output "Configured default: API $ApiPort / Web $WebPort; mode=$Mode"
    foreach ($port in @(@($config.managedPortsToInspect) + @($ApiPort,$WebPort) | Select-Object -Unique)) {
        $listeners = @(Get-ListenerInfo ([int]$port))
        if (-not $listeners) { Write-Output "PORT ${port}: free"; continue }
        foreach ($listener in $listeners) {
            Write-Output ("PORT {0}: PID={1}; exe={2}; started={3}; command={4}" -f $port,$listener.Pid,$listener.Executable,$listener.StartedUtc,$listener.CommandLine)
            if ($port -in @($ApiPort,8013,8011)) {
                $health = Get-ApiHealth ([int]$port); $version = Get-ApiVersion ([int]$port)
                Write-Output ("  API health={0}; version={1}; schema={2}; build={3}" -f $(if ($health) {$health.status} else {'unavailable'}),$(if ($version) {$version.version} else {'unknown'}),$(if ($version) {$version.schema_version} else {'unknown'}),$(if ($version) {$version.build_id} else {'unknown'}))
                if (-not $version -or -not $version.build_id) { Write-Output '  API compatibility: old or unrecognized API (version/build endpoint missing).' }
            }
            if ($port -in @($WebPort,5182,5181)) {
                $web = Get-WebInfo ([int]$port)
                Write-Output ("  Web HTTP={0}; build={1}; proxy API version={2}; proxy build={3}" -f $(if ($web) {$web.StatusCode} else {'unavailable'}),$(if ($web) {$web.BuildId} else {'unknown'}),$(if ($web -and $web.ProxiedApi) {$web.ProxiedApi.version} else {'unavailable'}),$(if ($web -and $web.ProxiedApi) {$web.ProxiedApi.build_id} else {'unavailable'}))
                if ($web -and -not $web.BuildId) { Write-Output '  Web compatibility: legacy/unmarked frontend build.' }
            }
        }
    }
    $directVersion = Get-ApiVersion $ApiPort
    $webCheck = Get-WebInfo $WebPort
    if (-not $directVersion -or -not $webCheck -or -not $webCheck.ProxiedApi) {
        Write-Output 'Proxy/API alignment: unavailable (one side has no version endpoint).'
    } elseif ($directVersion.build_id -ne $webCheck.ProxiedApi.build_id -or $directVersion.version -ne $webCheck.ProxiedApi.version) {
        Write-Output 'Proxy/API alignment: MISMATCH; the Web proxy is serving a different API process.'
    } else { Write-Output 'Proxy/API alignment: match.' }
    foreach ($file in Get-ManagedStateFiles) {
        try {
            $state = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
            Write-Output "MANAGED $($file.Name): mode=$($state.mode); API=$($state.apiPort); Web=$($state.webPort)"
            foreach ($entry in $state.processes) {
                $legacy = ($file.Name -ne 'papertrail.json' -and [string]::Equals([string]$state.root,$repo,[StringComparison]::OrdinalIgnoreCase))
                $portForEntry = [int]$(if ($entry.name -eq 'api') {$state.apiPort} else {$state.webPort})
                $identity = Get-ProcessIdentity $entry $portForEntry -AllowLegacyManifest:$legacy
                Write-Output "  $($entry.name) PID=$($entry.pid); exe=$($entry.executable); command=$($entry.commandLine); startUtcTicks=$($entry.startUtcTicks); identity=$($identity.Reason)"
            }
        } catch { Write-Output "MANAGED $($file.Name): manifest unreadable" }
    }
}

function Invoke-Doctor {
    $python = Get-ResolvedPython
    $node = Get-NodeCommand
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    Write-Output "PowerShell: $($PSVersionTable.PSVersion); edition=$($PSVersionTable.PSEdition)"
    $policy = Get-ExecutionPolicySummary
    Write-Output "ExecutionPolicy (read-only): process=$($policy.Process); MachinePolicy=$($policy.MachinePolicy); UserPolicy=$($policy.UserPolicy)"
    Write-Output "PATH key casing aliases in this process: $(Get-PathAliasCount)"
    Write-Output "Python: $((& $python --version 2>&1 | Out-String).Trim()); path=$python"
    Write-Output "Node: $((& $node --version 2>&1 | Out-String).Trim()); path=$node"
    if ($npm) { Write-Output "npm: $((& $npm.Source --version 2>&1 | Out-String).Trim())" } else { Write-Output 'npm.cmd: missing' }
    $savedPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = @((Join-Path $repo '.vendor'),$apiDir) -join ';'
    try {
        $pythonDeps = & $python -c 'import fastapi, sqlalchemy, pymupdf, httpx; print(fastapi.__version__, sqlalchemy.__version__, pymupdf.__version__, httpx.__version__)' 2>&1
        Write-Output "API dependencies (FastAPI SQLAlchemy PyMuPDF HTTPX): $($pythonDeps -join ' ')"
        if ($LASTEXITCODE -ne 0) { Write-Output 'API dependency status: missing or import failed' }
    } finally { $env:PYTHONPATH = $savedPythonPath }
    if (Test-Path -LiteralPath (Join-Path $webDir 'node_modules')) {
        $vitePackage = Get-Content -LiteralPath (Join-Path $webDir 'node_modules\vite\package.json') -Raw | ConvertFrom-Json
        $vuePackage = Get-Content -LiteralPath (Join-Path $webDir 'node_modules\vue\package.json') -Raw | ConvertFrom-Json
        Write-Output "Web dependencies: Vite=$($vitePackage.version); Vue=$($vuePackage.version)"
    } else { Write-Output 'Web dependencies: node_modules missing; run npm.cmd ci' }
    $safeNoProxy = Get-SafeProxyHosts
    Write-Output "NO_PROXY: preserved in parent process; DeepSeek bypass=$(Test-RequiredProxyBypass 'api.deepseek.com' $safeNoProxy); Bailian bypass=$(Test-RequiredProxyBypass 'dashscope.aliyuncs.com' $safeNoProxy); entries=$($safeNoProxy.Count) (values hidden)"
    foreach ($name in @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY')) {
        $exists = [bool][Environment]::GetEnvironmentVariable($name, 'Process')
        Write-Output "$name present in parent process: $exists (value hidden)"
    }
    foreach ($port in @($ApiPort,$WebPort,8013,5182)) {
        $info = @(Get-ListenerInfo $port)
        if (-not $info) { Write-Output "Port ${port}: free" }
        else { foreach ($item in $info) { Write-Output ("Port {0}: occupied PID={1}; exe={2}; command={3}" -f $port,$item.Pid,$item.Executable,$item.CommandLine) } }
    }
    $health = Get-ApiHealth $ApiPort
    $version = Get-ApiVersion $ApiPort
    Write-Output "API $ApiPort health: $(if ($health) { $health.status + '/' + $health.database } else { 'unavailable' })"
    Write-Output "API version/schema: $(if ($version) { $version.version + '/' + $version.schema_version } else { 'unavailable' })"
    Write-Output 'Doctor performs local inspection only; Chat/Embedding calls=0.'
}

function Invoke-SafeStop {
    $files = @(Get-ManagedStateFiles)
    if (-not $files) { Write-Output 'No PaperTrail process manifests found.'; return }
    foreach ($file in $files) {
        try { $state = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json }
        catch { Write-Output "Cannot read $($file.Name); left unchanged."; continue }
        $results = @()
        foreach ($entry in $state.processes) {
            $legacy = ($file.Name -ne 'papertrail.json' -and [string]::Equals([string]$state.root,$repo,[StringComparison]::OrdinalIgnoreCase))
            $portForEntry = [int]$(if ($entry.name -eq 'api') {$state.apiPort} else {$state.webPort})
            $results += [pscustomobject]@{Name=$entry.name;Pid=$entry.pid;Result=(Stop-VerifiedEntry $entry -AllowLegacyManifest:$legacy -ExpectedPort $portForEntry)}
        }
        $results | Format-Table -AutoSize | Out-String | Write-Output
        $unsafe = @($results | Where-Object { $_.Result -notin @('stopped-owned-process','process-exited') })
        if (-not $unsafe) { Remove-Item -LiteralPath $file.FullName -Force }
        else { Write-Output "Kept $($file.Name) because one or more process identities could not be safely stopped." }
    }
}

function Stop-ObsoleteManagedServices {
    foreach ($file in Get-ManagedStateFiles) {
        if ([string]::Equals([IO.Path]::GetFullPath($file.FullName),[IO.Path]::GetFullPath($runtimeFile),[StringComparison]::OrdinalIgnoreCase)) { continue }
        try { $state = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json }
        catch { throw "发现无法读取的 PaperTrail 旧运行清单 $($file.Name)；先运行 status 人工检查，不创建新服务。" }
        $unverifiedListeners = @()
        foreach ($entry in $state.processes) {
            $legacy = ($file.Name -ne 'papertrail.json' -and [string]::Equals([string]$state.root,$repo,[StringComparison]::OrdinalIgnoreCase))
            $port = [int]$(if ($entry.name -eq 'api') {$state.apiPort} else {$state.webPort})
            $identity = Get-ProcessIdentity $entry $port -AllowLegacyManifest:$legacy
            if ($identity.Valid) {
                $result = Stop-VerifiedEntry $entry -AllowLegacyManifest:$legacy -ExpectedPort $port
                if ($result -ne 'stopped-owned-process') {
                    $unverifiedListeners += @(Get-ListenerInfo $port)
                }
            } elseif ($identity.Reason -ne 'process-exited') {
                $unverifiedListeners += @(Get-ListenerInfo $port)
            }
        }
        if ($unverifiedListeners.Count -gt 0) {
            $details = $unverifiedListeners | ForEach-Object { "port=$($_.Port); PID=$($_.Pid); exe=$($_.Executable); command=$($_.CommandLine)" }
            throw "旧 PaperTrail 服务仍占用端口，且无法确认安全停止；为避免并行实例，本次不启动新服务。`n$($details -join "`n")"
        }
        $active = @($state.processes | Where-Object { (Get-ProcessIdentity $_).Reason -notin @('process-exited') })
        if (-not $active) { Remove-Item -LiteralPath $file.FullName -Force }
    }
}

function Invoke-Start {
    if ($ApiPort -eq $WebPort) { throw 'API 与 Web 不能使用相同端口。' }
    $python = Get-ResolvedPython
    $node = Get-NodeCommand
    $viteCli = Join-Path $webDir 'node_modules\vite\bin\vite.js'
    if (-not (Test-Path -LiteralPath $viteCli)) { throw 'apps/web/node_modules 缺失；先运行 npm.cmd ci。' }
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    Stop-ObsoleteManagedServices

    if (Test-Path -LiteralPath $runtimeFile) {
        $existing = Get-Content -LiteralPath $runtimeFile -Raw | ConvertFrom-Json
        $valid = @($existing.processes | ForEach-Object { Get-ProcessIdentity $_ } | Where-Object { -not $_.Valid })
        if (-not $valid) {
            if ($existing.apiPort -eq $ApiPort -and $existing.webPort -eq $WebPort -and $existing.mode -eq $Mode) {
                $health = Get-ApiHealth $ApiPort; $web = Get-WebInfo $WebPort
                if ($health -and $health.status -eq 'ok' -and $web -and $web.ProxiedApi -and $web.ProxiedApi.version -eq $health.version -and $web.ProxiedApi.build_id -eq $health.build_id) {
                    Write-Output "PaperTrail already running: API http://127.0.0.1:$ApiPort; Web http://127.0.0.1:$WebPort; mode=$Mode; no duplicate created."
                    return
                }
                throw '受管服务仍匹配，但健康或代理版本检查未通过。为避免重复实例，请显式运行 restart。'
            }
            throw "PaperTrail 已由管理器运行：API $($existing.apiPort)、Web $($existing.webPort)、mode=$($existing.mode)。start 不会静默切换模式或端口；如需切换，请显式运行 restart -Mode $Mode -ApiPort $ApiPort -WebPort $WebPort。"
        }
        Invoke-SafeStop
    }
    foreach ($port in @($ApiPort,$WebPort)) {
        $listeners = @(Get-ListenerInfo $port)
        if ($listeners) {
            $details = $listeners | ForEach-Object { "PID=$($_.Pid); exe=$($_.Executable); command=$($_.CommandLine); started=$($_.StartedUtc)" }
            throw "端口 $port 被未确认归属的进程占用。未终止任何进程。`n$($details -join "`n")`n请在 status 核实后选择其他端口，或在普通 PowerShell 中安全停止已确认的 PaperTrail 实例。"
        }
        if (-not (Test-PortFree $port)) { throw "端口 $port 已被占用，但当前环境无法读取其监听 PID。未启动或终止任何进程；请运行 status/doctor 或改用空闲端口。" }
    }

    $buildId = 'pt-' + [guid]::NewGuid().ToString('N')
    $expectedWebBuildId = ''
    if ($Mode -eq 'preview') {
        if (Test-SourceNewerThanBuild) {
            Write-Output '前端源码比 dist 更新或 dist 不存在；正在运行 Vue 类型检查与生产构建。'
            Push-Location $webDir
            try {
                $env:VITE_BUILD_ID = $buildId
                $savedErrorActionPreference = $ErrorActionPreference
                try {
                    $ErrorActionPreference = 'Continue'
                    $buildOutput = @(& npm.cmd run build 2>&1)
                    $buildExit = $LASTEXITCODE
                } finally { $ErrorActionPreference = $savedErrorActionPreference }
                $buildOutput | ForEach-Object { Write-Output $_ }
                if ($buildExit -ne 0) {
                    if (($buildOutput -join [Environment]::NewLine) -match 'spawn EPERM') { throw '生产构建被当前执行环境限制（Node/esbuild spawn EPERM）。请在普通 Windows PowerShell 运行 preview；没有修改安全策略。' }
                    throw "前端生产构建失败，exit=$buildExit。"
                }
            } finally { Pop-Location }
            $expectedWebBuildId = $buildId
        } else {
            Write-Output '生产构建新于源码；复用当前 dist。'
            $distHtml = Get-Content -LiteralPath (Join-Path $webDir 'dist\index.html') -Raw
            if ($distHtml -match 'name="papertrail-build" content="([^"]+)"') { $expectedWebBuildId = $Matches[1] }
            if (-not $expectedWebBuildId) { throw 'dist 缺少 PaperTrail 构建标识；请清理过期 dist 后使用 preview 重建。' }
        }
    }

    $proxyEntries = Get-SafeProxyHosts
    $pythonParts = @((Join-Path $repo '.vendor'), $apiDir)
    if ($env:PYTHONPATH) { $pythonParts += $env:PYTHONPATH.Split(';') | Where-Object { $_ } }
    $proxyTarget = "http://127.0.0.1:$ApiPort"
    $env:PYTHONPATH = @($pythonParts | Select-Object -Unique) -join ';'
    $env:NO_PROXY = $proxyEntries -join ','
    $env:no_proxy = $env:NO_PROXY
    $env:VITE_API_PROXY_TARGET = $proxyTarget
    $env:VITE_BUILD_ID = $buildId
    $env:PAPERTRAIL_API_BUILD_ID = $buildId
    Normalize-LauncherProcessPath
    $apiOut = Join-Path $logDir "api-$ApiPort.out.log"
    $apiErr = Join-Path $logDir "api-$ApiPort.err.log"
    $webOut = Join-Path $logDir "web-$WebPort.out.log"
    $webErr = Join-Path $logDir "web-$WebPort.err.log"
    $started = @()
    try {
        $apiArgs = @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port',"$ApiPort",'--no-access-log')
        $apiProc = Start-Process -FilePath $python -ArgumentList $apiArgs -WorkingDirectory $repo -WindowStyle Hidden -PassThru -RedirectStandardOutput $apiOut -RedirectStandardError $apiErr
        $apiCmdLine = "`"$python`" -m uvicorn app.main:app --host 127.0.0.1 --port $ApiPort --no-access-log"
        $started += [pscustomobject]@{name='api';pid=$apiProc.Id;startUtcTicks=$apiProc.StartTime.ToUniversalTime().Ticks;executable=[IO.Path]::GetFullPath($python);commandLine=$apiCmdLine}
        Wait-ProcessHttp $apiProc "http://127.0.0.1:$ApiPort/api/v1/health" 'API'
        $apiHealth = Get-ApiHealth $ApiPort
        $apiVersion = Get-ApiVersion $ApiPort
        if (-not $apiHealth -or $apiHealth.database -ne 'ok' -or -not $apiVersion -or $apiVersion.version -ne '0.2.0') { throw 'API 响应版本或数据库健康状态不匹配。' }

        if ($Mode -eq 'dev') { $webArgs = @($viteCli,'--host','127.0.0.1','--port',"$WebPort",'--strictPort') }
        else { $webArgs = @($viteCli,'preview','--configLoader','runner','--host','127.0.0.1','--port',"$WebPort",'--strictPort') }
        $webProc = Start-Process -FilePath $node -ArgumentList $webArgs -WorkingDirectory $webDir -WindowStyle Hidden -PassThru -RedirectStandardOutput $webOut -RedirectStandardError $webErr
        $webCmdLine = '"' + $node + '" "' + $viteCli + '" ' + ($webArgs[1..($webArgs.Length - 1)] -join ' ')
        $started += [pscustomobject]@{name='web';pid=$webProc.Id;startUtcTicks=$webProc.StartTime.ToUniversalTime().Ticks;executable=[IO.Path]::GetFullPath($node);commandLine=$webCmdLine}
        Wait-ProcessHttp $webProc "http://127.0.0.1:$WebPort/" 'Web'
        $webInfo = Get-WebInfo $WebPort
        if (-not $webInfo -or -not $webInfo.ProxiedApi -or $webInfo.ProxiedApi.version -ne $apiVersion.version -or $webInfo.ProxiedApi.build_id -ne $apiVersion.build_id) { throw 'Web 页面未通过代理连接到本次 API build ID。' }
        if ($Mode -eq 'preview' -and $webInfo.BuildId -ne $expectedWebBuildId) { throw 'Preview 返回的构建标识与预期构建不匹配。' }
        $state = [pscustomobject]@{root=$repo;mode=$Mode;apiPort=$ApiPort;webPort=$WebPort;apiVersion=$apiVersion.version;schemaVersion=$apiVersion.schema_version;webBuildId=$webInfo.BuildId;createdAt=[DateTime]::UtcNow.ToString('o');processes=$started}
        $state | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $runtimeFile -Encoding utf8
        Write-Output "PaperTrail started: mode=$Mode; API=http://127.0.0.1:$ApiPort (PID $($apiProc.Id), v$($apiVersion.version), schema $($apiVersion.schema_version)); Web=http://127.0.0.1:$WebPort (PID $($webProc.Id), build $($webInfo.BuildId)); proxy=$proxyTarget; model-host NO_PROXY bypasses verified; Chat/Embedding calls=0."
    } catch {
        foreach ($entry in $started) { $result = Stop-JustCreatedProcess $entry; Write-Output "Startup cleanup $($entry.name) PID $($entry.pid): $result" }
        throw
    }
}

function Wait-ProcessHttp($Process, [string]$Uri, [string]$Name) {
    $deadline = (Get-Date).AddSeconds([Math]::Max(1,$HealthTimeoutSeconds))
    do {
        $Process.Refresh()
        if ($Process.HasExited) {
            $portForLog = if ($Name -eq 'API') { $ApiPort } else { $WebPort }
            $errLog = Join-Path $logDir "$(if ($Name -eq 'API') { 'api' } else { 'web' })-$portForLog.err.log"
            $tail = if (Test-Path -LiteralPath $errLog) { (Get-Content -LiteralPath $errLog -Tail 16 -ErrorAction SilentlyContinue) -join [Environment]::NewLine } else { '' }
            if ($tail -match 'spawn EPERM|EPERM') {
                throw "$Name 进程启动受限：Node/Vite 子进程调用返回 spawn EPERM。这是当前执行环境限制的特征；请在普通 Windows PowerShell 运行统一入口。本脚本没有修改执行策略或系统安全设置。`n$tail"
            }
            throw "$Name 进程已退出（exit=$($Process.ExitCode)）。查看诊断：`n$tail"
        }
        try {
            $response = Invoke-WebRequest -Uri $Uri -TimeoutSec 2 -UseBasicParsing
            if ($response.StatusCode -eq 200) { return }
        } catch { }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    throw "$Name 在 $HealthTimeoutSeconds 秒内未就绪：$Uri。查看 var/logs 中对应日志；未触发模型调用。"
}

switch ($Action) {
    'start' { Invoke-Start }
    'status' { Show-Status }
    'stop' { Invoke-SafeStop }
    'restart' { Invoke-SafeStop; Invoke-Start }
    'doctor' { Invoke-Doctor }
}
