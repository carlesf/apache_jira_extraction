# 02-Measure-ProjectCoverage.ps1
# ---------------------------------------------------------------------------
# For each candidate project, compute:
#   - Total issues in the time window
#   - Fraction with Story Points populated
#   - Fraction with Original Estimate populated
#   - Fraction with Blocks/Depends links
#
# Uses maxResults=0 for every query: no issues are downloaded, only totals.
# Output: extract_out/project_coverage.csv
# ---------------------------------------------------------------------------

$ErrorActionPreference = "Stop"

$baseUrl   = "https://issues.apache.org/jira"
$outDir    = "./extract_out"
$outFile   = Join-Path $outDir "project_coverage.csv"
$delayMs   = 400

New-Item -ItemType Directory -Force -Path $outDir | Out-Null

# Candidate projects: modern-Agile Apache projects with known high activity.
# Edit this list to widen or narrow the search.
$candidates = @(
    "SPARK", "KAFKA", "FLINK", "BEAM", "AIRFLOW",
    "CASSANDRA", "ARROW", "PULSAR", "HIVE", "HADOOP",
    "HDFS", "HBASE", "LUCENE", "SOLR", "CALCITE"
)

$fromDate  = "2018-01-01"
$toDate    = "2024-01-01"
$typeFilter = 'issuetype in (Bug, Story, Task, "New Feature", Improvement, Epic)'

function Get-Count {
    param([string]$Jql)
    $encoded = [uri]::EscapeDataString($Jql)
    $url = "$baseUrl/rest/api/2/search?jql=$encoded&maxResults=0"
    try {
        $r = curl.exe -s $url | ConvertFrom-Json
        if ($null -ne $r.total) { return [int]$r.total }
        return -1
    } catch {
        return -1
    }
}

Write-Host ("Measuring coverage for {0} projects in window {1} .. {2}`n" -f $candidates.Count, $fromDate, $toDate)

$results = @()
foreach ($p in $candidates) {
    $base = "project = $p AND created >= `"$fromDate`" AND created < `"$toDate`" AND $typeFilter"

    $total = Get-Count $base
    Start-Sleep -Milliseconds $delayMs
    $sp    = Get-Count "$base AND `"Story Points`" is not EMPTY"
    Start-Sleep -Milliseconds $delayMs
    $est   = Get-Count "$base AND timeoriginalestimate is not EMPTY"
    Start-Sleep -Milliseconds $delayMs
    $links = Get-Count "$base AND issueLinkType in (Blocks, `"is blocked by`", Depends, `"depends on`")"
    Start-Sleep -Milliseconds $delayMs

    $spRate   = if ($total -gt 0) { [math]::Round($sp    / $total, 3) } else { 0 }
    $estRate  = if ($total -gt 0) { [math]::Round($est   / $total, 3) } else { 0 }
    $linkRate = if ($total -gt 0) { [math]::Round($links / $total, 3) } else { 0 }

    $results += [PSCustomObject]@{
        Project       = $p
        Total         = $total
        SP_Populated  = $sp
        SP_Rate       = $spRate
        Est_Populated = $est
        Est_Rate      = $estRate
        Linked        = $links
        Link_Rate     = $linkRate
    }

    Write-Host ("  {0,-10} total={1,7:N0}  sp={2,6:N0} ({3,6:P1})  est={4,6:N0} ({5,6:P1})  links={6,6:N0} ({7,6:P1})" -f `
        $p, $total, $sp, $spRate, $est, $estRate, $links, $linkRate)
}

$results | Sort-Object SP_Rate -Descending | Export-Csv -Path $outFile -NoTypeInformation -Encoding UTF8

Write-Host "`nCoverage saved to $outFile"
Write-Host "Selection rule of thumb: SP_Rate >= 0.30 AND Link_Rate >= 0.10"
