# 01-Discover-CustomFields.ps1
# ---------------------------------------------------------------------------
# Queries the Apache Jira REST API to map human-readable field names to their
# instance-specific IDs (e.g., "Story Points" -> "customfield_12310243").
# Must be run once before any extraction or reconstruction.
# ---------------------------------------------------------------------------

$ErrorActionPreference = "Stop"

$baseUrl = "https://issues.apache.org/jira"
$outDir  = "./extract_out"
$outFile = Join-Path $outDir "field_map.json"

New-Item -ItemType Directory -Force -Path $outDir | Out-Null

# Field names we care about. Built-in fields like timeoriginalestimate are NOT
# here — they have fixed IDs and do not appear in the customfield list.
$targetNames = @(
    "Story Points",
    "Epic Link",
    "Epic Name",
    "Sprint",
    "Rank",
    "Target Version",
    "Fix Version/s"
)

Write-Host "Fetching field metadata from $baseUrl ..."
$raw = curl.exe -s "$baseUrl/rest/api/2/field"
$fields = $raw | ConvertFrom-Json

$map = @{}
foreach ($f in $fields) {
    if ($targetNames -contains $f.name) {
        $map[$f.name] = $f.id
        Write-Host ("  {0,-22} -> {1}" -f $f.name, $f.id)
    }
}

if ($map.Count -eq 0) {
    Write-Error "No target fields resolved. Check network access to $baseUrl."
    exit 1
}

# Write BOMless UTF-8 so downstream parsers do not choke.
$json = $map | ConvertTo-Json
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText((Resolve-Path $outDir).Path + "/field_map.json", $json, $utf8NoBom)

Write-Host "`nField map written to $outFile"
Write-Host ("Resolved {0} of {1} target fields." -f $map.Count, $targetNames.Count)
