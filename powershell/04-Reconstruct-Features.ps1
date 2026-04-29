# 04-Reconstruct-Features.ps1
# ---------------------------------------------------------------------------
# Reads raw per-issue JSON files from extract_out/raw/<PROJECT>/ and produces:
#   - extract_out/clean/<PROJECT>.jsonl         (one issue per line, final schema)
#   - extract_out/clean/<PROJECT>.summary.csv   (population rates for sanity checks)
#
# Core rule: for time-varying fields (priority, story points, ...), the value
# at creation is the `fromString` of the oldest change-history entry for that
# field. If no history entry exists for the field, the current value IS the
# creation value.
#
# Usage:
#   .\04-Reconstruct-Features.ps1 -Project SPARK
# ---------------------------------------------------------------------------

param(
    [Parameter(Mandatory=$true)] [string]$Project
)

$ErrorActionPreference = "Stop"

$inDir    = "./extract_out/raw/$Project"
$cleanDir = "./extract_out/clean"
$outJsonl = Join-Path $cleanDir "$Project.jsonl"
$outSum   = Join-Path $cleanDir "$Project.summary.csv"
$fieldMapPath = "./extract_out/field_map.json"

if (-not (Test-Path $inDir))    { throw "Raw directory not found: $inDir" }
if (-not (Test-Path $fieldMapPath)) { throw "Field map not found. Run 01-Discover-CustomFields.ps1 first." }

New-Item -ItemType Directory -Force -Path $cleanDir | Out-Null

$fieldMap = Get-Content $fieldMapPath -Raw | ConvertFrom-Json
$storyPointsField = $fieldMap."Story Points"
$epicLinkField    = $fieldMap."Epic Link"

$utf8NoBom = New-Object System.Text.UTF8Encoding $false

# ---------- Helpers ----------------------------------------------------------

# Return the value at creation time for a given field, using changelog history.
function Get-ValueAtCreation {
    param($Issue, [string]$FieldName, $CurrentValue)

    $oldestChange = $null
    if ($Issue.changelog -and $Issue.changelog.histories) {
        foreach ($h in $Issue.changelog.histories) {
            foreach ($it in $h.items) {
                if ($it.field -eq $FieldName -or $it.fieldId -eq $FieldName) {
                    $created = [datetime]$h.created
                    if ($null -eq $oldestChange -or $created -lt $oldestChange.Created) {
                        $oldestChange = [PSCustomObject]@{
                            Created = $created
                            From    = $it.fromString
                        }
                    }
                }
            }
        }
    }
    if ($null -eq $oldestChange) { return $CurrentValue }
    return $oldestChange.From
}

# Find when a particular link to a target key was added, by scanning "Link" history entries.
function Get-LinkAddedAt {
    param($Issue, [string]$TargetKey)
    if (-not $Issue.changelog -or -not $Issue.changelog.histories) { return $null }
    foreach ($h in $Issue.changelog.histories) {
        foreach ($it in $h.items) {
            if ($it.field -eq "Link") {
                $s = "$($it.toString) $($it.fromString)"
                if ($s -match [regex]::Escape($TargetKey)) {
                    return $h.created
                }
            }
        }
    }
    return $null
}

function Get-SHA256Hex {
    param([string]$Value)
    if ([string]::IsNullOrEmpty($Value)) { return $null }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Value))
        return -join ($bytes | ForEach-Object { "{0:x2}" -f $_ })
    } finally {
        $sha.Dispose()
    }
}

# Access a dynamic property by name, returning $null if not present.
function Get-DynamicProp {
    param($Obj, [string]$Name)
    if ($null -eq $Obj) { return $null }
    $prop = $Obj.PSObject.Properties[$Name]
    if ($null -eq $prop) { return $null }
    return $prop.Value
}

# ---------- Main loop --------------------------------------------------------

# Reset outputs.
if (Test-Path $outJsonl) { Remove-Item $outJsonl }

$files = Get-ChildItem -Path $inDir -Filter "*.json" -File
Write-Host ("Processing {0} issues from {1} ..." -f $files.Count, $inDir)

# Running counters for the summary CSV.
$stats = @{
    total                 = 0
    has_priority          = 0
    has_story_points      = 0
    has_original_estimate = 0
    has_parent            = 0
    has_links             = 0
    has_subtasks          = 0
    has_components        = 0
    has_labels            = 0
    has_description       = 0
}

# Stream to JSONL in append mode (BOMless).
$sw = New-Object System.IO.StreamWriter($outJsonl, $false, $utf8NoBom)
try {
    $i = 0
    foreach ($file in $files) {
        $i++
        $issue = Get-Content $file.FullName -Raw | ConvertFrom-Json
        $f = $issue.fields

        $stats.total++

        # Current -> reconstructed value for priority.
        $curPriority = if ($f.priority) { $f.priority.name } else { $null }
        $priorityAtCreation = Get-ValueAtCreation $issue "priority" $curPriority

        # Story points (customfield).
        $storyPoints = $null
        if ($storyPointsField) {
            $curSP = Get-DynamicProp $f $storyPointsField
            $storyPoints = Get-ValueAtCreation $issue $storyPointsField $curSP
            if ($storyPoints) { $stats.has_story_points++ }
        }

        # Parent: either subtask parent or Epic Link.
        $parentKey = $null
        if ($f.parent) {
            $parentKey = $f.parent.key
        } elseif ($epicLinkField) {
            $el = Get-DynamicProp $f $epicLinkField
            if ($el) { $parentKey = $el }
        }
        if ($parentKey) { $stats.has_parent++ }

        # Issue links.
        $links = @()
        if ($f.issuelinks) {
            foreach ($l in $f.issuelinks) {
                if ($l.outwardIssue) {
                    $target = $l.outwardIssue.key
                    $direction = "outward"
                } elseif ($l.inwardIssue) {
                    $target = $l.inwardIssue.key
                    $direction = "inward"
                } else {
                    continue
                }
                $addedAt = Get-LinkAddedAt $issue $target
                $links += [PSCustomObject]@{
                    type      = $l.type.name
                    direction = $direction
                    target    = $target
                    added_at  = $addedAt
                }
            }
        }
        if ($links.Count -gt 0) { $stats.has_links++ }

        # Subtasks.
        $subtasks = @()
        if ($f.subtasks) {
            foreach ($st in $f.subtasks) { $subtasks += $st.key }
        }
        if ($subtasks.Count -gt 0) { $stats.has_subtasks++ }

        # Components and labels.
        $components = @()
        if ($f.components) { foreach ($c in $f.components) { $components += $c.name } }
        if ($components.Count -gt 0) { $stats.has_components++ }

        $labels = @()
        if ($f.labels) { foreach ($lb in $f.labels) { $labels += $lb } }
        if ($labels.Count -gt 0) { $stats.has_labels++ }

        $affectsVersions = @()
        if ($f.versions) { foreach ($v in $f.versions) { $affectsVersions += $v.name } }

        if ($priorityAtCreation) { $stats.has_priority++ }
        if ($f.timeoriginalestimate) { $stats.has_original_estimate++ }
        if ($f.description) { $stats.has_description++ }

        # Reporter anonymisation.
        $reporterAnon = $null
        if ($f.reporter) {
            $id = if ($f.reporter.accountId) { $f.reporter.accountId } else { $f.reporter.name }
            $reporterAnon = Get-SHA256Hex $id
        }

        # Build ordered output record.
        $record = [ordered]@{
            key                   = $issue.key
            project               = $f.project.key
            created_at            = $f.created
            type                  = if ($f.issuetype) { $f.issuetype.name } else { $null }
            title                 = $f.summary
            description           = $f.description
            components            = $components
            labels                = $labels
            parent_key            = $parentKey
            affects_versions      = $affectsVersions
            reporter_anon         = $reporterAnon
            priority_at_creation  = $priorityAtCreation
            story_points_planning = $storyPoints
            original_estimate_sec = $f.timeoriginalestimate
            dependency_links      = $links
            subtasks              = $subtasks
        }

        $line = $record | ConvertTo-Json -Depth 10 -Compress
        $sw.WriteLine($line)

        if ($i % 500 -eq 0) {
            Write-Host ("  processed {0}/{1}" -f $i, $files.Count)
        }
    }
}
finally {
    $sw.Close()
}

# ---------- Summary CSV ------------------------------------------------------

$total = [double]$stats.total
$rows = @(
    [PSCustomObject]@{ Field = "total_issues";          Count = $stats.total;                 Rate = 1.0 }
    [PSCustomObject]@{ Field = "has_description";       Count = $stats.has_description;       Rate = [math]::Round($stats.has_description       / $total, 3) }
    [PSCustomObject]@{ Field = "has_priority";          Count = $stats.has_priority;          Rate = [math]::Round($stats.has_priority          / $total, 3) }
    [PSCustomObject]@{ Field = "has_story_points";      Count = $stats.has_story_points;      Rate = [math]::Round($stats.has_story_points      / $total, 3) }
    [PSCustomObject]@{ Field = "has_original_estimate"; Count = $stats.has_original_estimate; Rate = [math]::Round($stats.has_original_estimate / $total, 3) }
    [PSCustomObject]@{ Field = "has_components";        Count = $stats.has_components;        Rate = [math]::Round($stats.has_components        / $total, 3) }
    [PSCustomObject]@{ Field = "has_labels";            Count = $stats.has_labels;            Rate = [math]::Round($stats.has_labels            / $total, 3) }
    [PSCustomObject]@{ Field = "has_parent";            Count = $stats.has_parent;            Rate = [math]::Round($stats.has_parent            / $total, 3) }
    [PSCustomObject]@{ Field = "has_links";             Count = $stats.has_links;             Rate = [math]::Round($stats.has_links             / $total, 3) }
    [PSCustomObject]@{ Field = "has_subtasks";          Count = $stats.has_subtasks;          Rate = [math]::Round($stats.has_subtasks          / $total, 3) }
)
$rows | Export-Csv -Path $outSum -NoTypeInformation -Encoding UTF8

Write-Host ""
Write-Host "Clean dataset : $outJsonl"
Write-Host "Summary       : $outSum"
Write-Host ""
$rows | Format-Table -AutoSize
