# 03-Extract-Issues.ps1
# ---------------------------------------------------------------------------
# Main extraction. Two passes:
#   1. Enumerate all matching issue keys via paginated JQL search.
#   2. For each key, fetch full issue + changelog, handling changelog pagination
#      for issues with > 100 history entries.
#
# Features:
#   - Resumable via .checkpoint.txt
#   - Exponential backoff on HTTP 429/5xx
#   - One JSON file per issue (easy to inspect, easy to resume)
#
# Usage:
#   .\03-Extract-Issues.ps1 -Project SPARK -FromDate 2018-01-01 -ToDate 2024-01-01
# ---------------------------------------------------------------------------

param(
    [Parameter(Mandatory=$true)] [string]$Project,
    [string]$FromDate = "2018-01-01",
    [string]$ToDate   = "2024-01-01",
    [int]$BatchSize   = 100,
    [int]$DelayMs     = 400
)

$ErrorActionPreference = "Stop"
$baseUrl = "https://issues.apache.org/jira"
$outDir  = "./extract_out/raw/$Project"
$chkFile = Join-Path $outDir ".checkpoint.txt"
$logFile = Join-Path $outDir ".extraction.log"
$tmpFile = Join-Path $env:TEMP "jira_resp_$Project.json"

New-Item -ItemType Directory -Force -Path $outDir | Out-Null

function Write-Log {
    param([string]$Msg, [string]$Level = "INFO")
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:ss"), $Level, $Msg
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

# Single GET with retries. Returns parsed JSON or $null on unrecoverable failure.
function Get-JiraJson {
    param([string]$Url, [int]$MaxRetries = 5)

    for ($attempt = 1; $attempt -le $MaxRetries; $attempt++) {
        try {
            $status = curl.exe -s -o $tmpFile -w "%{http_code}" $Url
            $code = [int]$status

            if ($code -eq 200) {
                return Get-Content -Path $tmpFile -Raw | ConvertFrom-Json
            }

            if ($code -eq 404) {
                Write-Log "404 (issue unavailable or deleted): $Url" "WARN"
                return $null
            }

            if ($code -in @(429, 502, 503, 504)) {
                $wait = [math]::Min(60, [math]::Pow(2, $attempt))
                Write-Log "HTTP $code on attempt $attempt. Waiting ${wait}s." "WARN"
                Start-Sleep -Seconds $wait
                continue
            }

            Write-Log "HTTP $code (not retrying): $Url" "ERROR"
            return $null
        }
        catch {
            $wait = [math]::Min(60, [math]::Pow(2, $attempt))
            Write-Log "Exception on attempt ${attempt}: $_. Waiting ${wait}s." "WARN"
            Start-Sleep -Seconds $wait
        }
    }

    Write-Log "Max retries exceeded: $Url" "ERROR"
    return $null
}

# Load checkpoint.
$done = @{}
if (Test-Path $chkFile) {
    Get-Content $chkFile | ForEach-Object {
        if ($_.Trim()) { $done[$_.Trim()] = $true }
    }
    Write-Log ("Resuming: {0} issues already extracted." -f $done.Count)
}

# -----------------------------------------------------------------------
# Pass 1: enumerate issue keys
# -----------------------------------------------------------------------
$typeFilter = 'issuetype in (Bug, Story, Task, "New Feature", Improvement, Epic)'
$jql = "project = $Project AND created >= `"$FromDate`" AND created < `"$ToDate`" AND $typeFilter ORDER BY created ASC"
$encoded = [uri]::EscapeDataString($jql)

Write-Log "Enumerating issues in project=$Project window=[$FromDate .. $ToDate]"

$allKeys = New-Object System.Collections.Generic.List[string]
$startAt = 0
$total   = -1

do {
    $url = "$baseUrl/rest/api/2/search?jql=$encoded&fields=key&maxResults=$BatchSize&startAt=$startAt"
    $page = Get-JiraJson $url
    if ($null -eq $page) { break }

    foreach ($issue in $page.issues) { $allKeys.Add($issue.key) }

    if ($total -lt 0) {
        $total = [int]$page.total
        Write-Log "Total matching issues: $total"
    }

    $startAt += $BatchSize
    Write-Log ("Enumerated {0}/{1}" -f [math]::Min($startAt, $total), $total)
    Start-Sleep -Milliseconds $DelayMs
} while ($startAt -lt $total)

Write-Log ("Enumeration complete: {0} keys collected." -f $allKeys.Count)

# -----------------------------------------------------------------------
# Pass 2: fetch each issue with full changelog
# -----------------------------------------------------------------------
$fetched = 0
$skipped = 0
$failed  = 0

foreach ($key in $allKeys) {
    if ($done.ContainsKey($key)) {
        $skipped++
        continue
    }

    $url = "$baseUrl/rest/api/2/issue/${key}?expand=changelog"
    $issue = Get-JiraJson $url
    if ($null -eq $issue) {
        $failed++
        continue
    }

    # Paginate changelog if truncated.
    if ($issue.changelog -and $issue.changelog.total -gt $issue.changelog.maxResults) {
        $histories = New-Object System.Collections.Generic.List[object]
        foreach ($h in $issue.changelog.histories) { $histories.Add($h) }

        $chStart = $issue.changelog.maxResults
        while ($chStart -lt $issue.changelog.total) {
            $chUrl = "$baseUrl/rest/api/2/issue/${key}/changelog?startAt=$chStart"
            $chPage = Get-JiraJson $chUrl
            if ($null -eq $chPage) { break }
            foreach ($h in $chPage.values) { $histories.Add($h) }
            $chStart += [int]$chPage.maxResults
            Start-Sleep -Milliseconds $DelayMs
        }
        $issue.changelog.histories = $histories.ToArray()
    }

    # Write BOMless UTF-8.
    $json = $issue | ConvertTo-Json -Depth 25
    $outPath = Join-Path $outDir "$key.json"
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($outPath, $json, $utf8NoBom)

    # Append to checkpoint (line by line, flush-safe).
    Add-Content -Path $chkFile -Value $key -Encoding UTF8

    $fetched++
    if ($fetched % 50 -eq 0) {
        Write-Log ("Fetched {0} / {1} (skipped={2}, failed={3})" -f $fetched, $allKeys.Count, $skipped, $failed)
    }

    Start-Sleep -Milliseconds $DelayMs
}

Write-Log ("Extraction complete. Fetched={0} Skipped={1} Failed={2}" -f $fetched, $skipped, $failed)
Write-Log "Raw files in: $outDir"
