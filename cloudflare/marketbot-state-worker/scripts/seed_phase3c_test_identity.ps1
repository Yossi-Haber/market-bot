[CmdletBinding()]
param([switch]$Cleanup)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'deployment_helpers.ps1')

$workerProject = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $workerProject 'wrangler.test.jsonc'
$templatePath = Join-Path $PSScriptRoot $(if ($Cleanup) { 'cleanup_phase3c_test_data.sql' } else { 'phase3c_test_identity.sql' })
$tempSql = $null
$secureSecret = $null
$bstr = [IntPtr]::Zero
$plainSecret = $null
$hashHex = $null
$pushed = $false

try {
    $null = Assert-IsolatedTestConfig -Path $configPath
    if (-not (Test-Path -LiteralPath $templatePath -PathType Leaf)) { throw 'TEST_IDENTITY_SQL_TEMPLATE_MISSING' }
    if (-not $env:LOCALAPPDATA) { throw 'USER_TEMP_DIRECTORY_NOT_FOUND' }
    $tempDirectory = Join-Path $env:LOCALAPPDATA 'Temp'
    if (-not (Test-Path -LiteralPath $tempDirectory -PathType Container)) { throw 'USER_TEMP_DIRECTORY_NOT_FOUND' }

    $secureSecret = Read-Host 'TEST Worker STATE_ADMIN_HMAC_SECRET (hidden; use password-manager TEST entry)' -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureSecret)
    $plainSecret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    if ([Text.Encoding]::UTF8.GetByteCount($plainSecret) -lt 32) { throw 'TEST_ADMIN_SECRET_INVALID' }
    foreach ($name in @('STATE_ADMIN_HMAC_SECRET', 'MARKETBOT_D1_STATE_ADMIN_HMAC_SECRET')) {
        $other = [Environment]::GetEnvironmentVariable($name)
        if ($other -and [string]::Equals($plainSecret, $other, [StringComparison]::Ordinal)) { throw 'TEST_SECRET_MATCHES_NON_TEST_CREDENTIAL' }
    }
    $hashBytes = [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($plainSecret))
    $hashHex = [Convert]::ToHexString($hashBytes).ToLowerInvariant()
    [Array]::Clear($hashBytes, 0, $hashBytes.Length)

    $tempSql = Join-Path $tempDirectory ('marketbot-phase3c-test-identity-{0}.sql' -f [guid]::NewGuid().ToString('N'))
    New-Item -ItemType File -Path $tempSql -ErrorAction Stop | Out-Null
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $acl = Invoke-ExternalCommand { & icacls.exe $tempSql /inheritance:r /grant:r ('*{0}:(F)' -f $sid) '*S-1-5-18:(F)' }
    if (-not $acl.Started -or -not $acl.Succeeded) { throw 'TEST_IDENTITY_TEMP_ACL_FAILED' }

    $sql = [IO.File]::ReadAllText($templatePath, [Text.Encoding]::UTF8)
    if (-not $sql.Contains('__TEST_ADMIN_SECRET_SHA256__')) { throw 'TEST_IDENTITY_SQL_TEMPLATE_INVALID' }
    $sql = $sql.Replace('__TEST_ADMIN_SECRET_SHA256__', $hashHex)
    [IO.File]::WriteAllText($tempSql, $sql, [Text.UTF8Encoding]::new($false))

    Push-Location -LiteralPath $workerProject
    $pushed = $true
    if ($Cleanup) {
        $identityQuery = 'SELECT database_id,worker_name,mode,attestation_hash FROM test_environment_identity WHERE singleton=1;'
        $identityCheck = Invoke-ExternalCommand { & npx.cmd wrangler d1 execute marketbot-test --remote --json --config .\wrangler.test.jsonc --command $identityQuery }
        if (-not $identityCheck.Started -or -not $identityCheck.Succeeded) { throw 'TEST_CLEANUP_IDENTITY_PREFLIGHT_FAILED' }
        $identityJson = (@($identityCheck.Output | ForEach-Object { [string]$_ }) -join "`n")
        try { $identityParsed = ConvertFrom-Json -InputObject $identityJson -AsHashtable -NoEnumerate -ErrorAction Stop }
        catch { throw 'TEST_CLEANUP_IDENTITY_PREFLIGHT_INVALID' }
        if ($identityParsed -isnot [System.Collections.IList] -or $identityParsed -is [string] -or
            $identityParsed.Count -ne 1 -or $identityParsed[0].success -ne $true -or
            $identityParsed[0].results -isnot [System.Collections.IList] -or $identityParsed[0].results.Count -ne 1) {
            throw 'TEST_CLEANUP_IDENTITY_PREFLIGHT_INVALID'
        }
        $identityRow = $identityParsed[0].results[0]
        $identityFields = @('database_id','worker_name','mode','attestation_hash')
        if ($identityRow -isnot [System.Collections.IDictionary] -or $identityRow.Count -ne $identityFields.Count) {
            throw 'TEST_CLEANUP_IDENTITY_PREFLIGHT_INVALID'
        }
        foreach ($field in $identityFields) {
            if (-not $identityRow.Contains($field) -or $null -eq $identityRow[$field]) { throw 'TEST_CLEANUP_IDENTITY_PREFLIGHT_INVALID' }
        }
        if ($identityRow.database_id -cne '8049a22c-eb1f-4e9d-928d-29b35dcc6f70' -or
            $identityRow.worker_name -cne 'marketbot-state-api-test' -or
            $identityRow.mode -cne 'phase3c-synthetic-mutations-v1' -or
            -not [string]::Equals([string]$identityRow.attestation_hash, $hashHex, [StringComparison]::Ordinal)) {
            throw 'TEST_CLEANUP_IDENTITY_ATTESTATION_MISMATCH'
        }
        Write-Host 'TEST_CLEANUP_IDENTITY_PREFLIGHT=PASS target=marketbot-test'
    }

    if ($Cleanup) { Write-Host 'TEST_CLEANUP_SQL_START target=marketbot-test' }
    $seed = Invoke-ExternalCommand { & npx.cmd wrangler d1 execute marketbot-test --remote --config .\wrangler.test.jsonc --file $tempSql }
    if (-not $seed.Started -or -not $seed.Succeeded) {
        if ($Cleanup) { Write-Error ("TEST_CLEANUP_SQL_FAILED started={0} exit_code={1}" -f $seed.Started, $(if ($null -eq $seed.ExitCode) { 'unknown' } else { $seed.ExitCode })) }
        throw 'TEST_IDENTITY_SEED_FAILED'
    }
    if ($Cleanup) { Write-Host ("TEST_CLEANUP_SQL_COMPLETED exit_code={0}" -f $seed.ExitCode) }

    $query = if ($Cleanup) {
        "SELECT (SELECT version FROM schema_meta WHERE singleton=1) AS schema_version, cloud_quota_exclusive, windows_sender_disabled, cloud_delivery_enabled, (SELECT COUNT(*) FROM operations WHERE operation_id LIKE '3c%') AS synthetic_operation_count, (SELECT COUNT(*) FROM quota_reservation WHERE reservation_id LIKE '3c%') AS synthetic_reservation_count, (SELECT COUNT(*) FROM run_claim WHERE market_date BETWEEN '2090-01-01' AND '2099-12-31' AND run_id LIKE '3c%') AS synthetic_run_count, (SELECT COUNT(*) FROM delivery_recipient WHERE market_date BETWEEN '2090-01-01' AND '2099-12-31' AND recipient_hmac IN ('hmac256:ac00581af76fae2942fefc401985c751c927c4412b3e8486f54cda8e4d420419','hmac256:ad4e4df2c02723b74f014769278cd2c1edac43e7f17751f1b9048f40b11aa692')) AS synthetic_delivery_count, (SELECT COUNT(*) FROM audit_event WHERE audit_id LIKE '3c%') AS synthetic_audit_count, (SELECT COUNT(*) FROM test_environment_identity) AS identity_count, (SELECT COUNT(*) FROM quota_bucket b WHERE b.used_credits < COALESCE((SELECT SUM(r.credits) FROM quota_reservation r WHERE (b.period='minute' AND r.utc_minute=b.bucket_key) OR (b.period='day' AND r.utc_day=b.bucket_key)),0)) AS quota_bucket_underflow_count FROM ownership WHERE singleton=1;"
    } else {
        'SELECT database_id,worker_name,mode,attestation_hash FROM test_environment_identity WHERE singleton=1;'
    }
    $check = Invoke-ExternalCommand { & npx.cmd wrangler d1 execute marketbot-test --remote --json --config .\wrangler.test.jsonc --command $query }
    if (-not $check.Started -or -not $check.Succeeded) { throw 'TEST_IDENTITY_READBACK_FAILED' }
    $json = (@($check.Output | ForEach-Object { [string]$_ }) -join "`n")
    try { $parsed = ConvertFrom-Json -InputObject $json -AsHashtable -NoEnumerate -ErrorAction Stop }
    catch { throw 'TEST_IDENTITY_READBACK_INVALID' }
    if ($parsed -isnot [System.Collections.IList] -or $parsed.Count -ne 1 -or $parsed[0].success -ne $true -or $parsed[0].results.Count -ne 1) { throw 'TEST_IDENTITY_READBACK_INVALID' }
    $row = $parsed[0].results[0]
    if ($Cleanup) {
        $expected = @('schema_version','cloud_quota_exclusive','windows_sender_disabled','cloud_delivery_enabled','synthetic_operation_count','synthetic_reservation_count','synthetic_run_count','synthetic_delivery_count','synthetic_audit_count','identity_count','quota_bucket_underflow_count')
        if ($row.Count -ne $expected.Count) { throw 'TEST_CLEANUP_READBACK_INVALID' }
        foreach ($field in $expected) {
            if (-not $row.Contains($field) -or $row[$field] -isnot [int] -and $row[$field] -isnot [long]) { throw 'TEST_CLEANUP_READBACK_INVALID' }
        }
        $failedChecks = [System.Collections.Generic.List[string]]::new()
        foreach ($field in @('schema_version','cloud_quota_exclusive','windows_sender_disabled','cloud_delivery_enabled','synthetic_operation_count','synthetic_reservation_count','synthetic_run_count','synthetic_delivery_count','synthetic_audit_count','identity_count','quota_bucket_underflow_count')) {
            $expectedValue = if ($field -eq 'schema_version') { 1 } else { 0 }
            if ($row[$field] -ne $expectedValue) { $failedChecks.Add($field) }
        }
        if ($failedChecks.Count -gt 0) {
            $safeObserved = ($expected | ForEach-Object { '{0}={1}' -f $_, $row[$_] }) -join ','
            throw ("TEST_CLEANUP_VALIDATION_FAILED failed_fields={0} observed={1}" -f ($failedChecks -join ','), $safeObserved)
        }
        Write-Host 'TEST_SYNTHETIC_ROWS_CLEANED=PASS'
        Write-Host 'TEST_IDENTITY_REMOVED=PASS; future mutations fail closed'
        Write-Host 'OWNERSHIP_GATES=0,0,0 unchanged'
    }
    else {
        if ($row.database_id -cne '8049a22c-eb1f-4e9d-928d-29b35dcc6f70' -or
            $row.worker_name -cne 'marketbot-state-api-test' -or
            $row.mode -cne 'phase3c-synthetic-mutations-v1' -or
            -not [string]::Equals([string]$row.attestation_hash, $hashHex, [StringComparison]::Ordinal)) {
            throw 'TEST_IDENTITY_READBACK_MISMATCH'
        }
        Write-Host 'TEST_IDENTITY_SEEDED=PASS (marketbot-test only; attestation value not displayed)'
        Write-Host 'OWNERSHIP_GATES=not modified by this script'
    }
}
catch {
    Write-Error ([string]$_.Exception.Message)
    exit 1
}
finally {
    if ($tempSql -and (Test-Path -LiteralPath $tempSql)) {
        try { Remove-Item -LiteralPath $tempSql -Force -ErrorAction Stop }
        catch { Write-Warning 'TEST_IDENTITY_TEMP_SQL_CLEANUP_FAILED' }
    }
    if ($bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    $plainSecret = $null
    $hashHex = $null
    $secureSecret = $null
    if ($pushed) { Pop-Location }
}
