<#
    Does PUT /api/history/{id}/contacts/{contactId} move an advisor out of the
    Act! grid's INVITEE column and into ASSOCIATED CONTACT?

    WHY A SCRIPT AND NOT THREE COMMANDS
    -----------------------------------
    The history id has to be carried from step one into step two, and copying a
    GUID by hand between two commands that both write to the CRM is exactly
    where a wrong record gets modified. This parses the id out of the first
    run's own output, so the record acted on is provably the one just created.

    WHAT IT DOES
    ------------
      1. Schedules an activity for Matt Keeter against YOUR OWN Act! contact and
         clears it, producing history the same way the app does.
      2. Runs the documented association endpoint against that record.
      3. Prints the id and the delete command.

    Your own contact is the target on purpose: the question is about which UI
    column a contact appears in, which does not need a real advisor's record to
    answer, and this way nothing lands on somebody we actually sell to.

    THE ANSWER IS NOT IN THIS OUTPUT. The API showed both kinds of association
    as identical when compared field by field, so the last step is a human
    looking at the Act! history grid. The script says so at the end.

    Usage:
        $env:ACT_PASSWORD = 'your password in single quotes'
        .\scripts\test_associate.ps1
#>

$ErrorActionPreference = "Stop"

$User = "bladyman@eicatlanta.com"
$Db   = "EQUITYINVESTMENT"
# Robert Ladyman's Act! contact. Passed explicitly rather than relying on
# myrecord so that the id used in step 2 is the same one step 1 wrote to.
$ContactId = "312d98a6-9d9e-4414-802d-f2bd31eded5c"
$ScheduleFor = "mkeeter@eicatlanta.com"

if (-not $env:ACT_PASSWORD) {
    Write-Host "ACT_PASSWORD is not set in this window." -ForegroundColor Yellow
    Write-Host "  `$env:ACT_PASSWORD = 'your password in single quotes'"
    exit 2
}

Write-Host "STEP 1 - create a history record the way the app does" -ForegroundColor Cyan
$create = python src/act_write_test.py --user $User --db $Db `
            --contact-id $ContactId --activity-route $ScheduleFor --confirm 2>&1
$create | Write-Host

# The delete commands at the end of the run each name a GUID this run created.
# Parsing those rather than the summary block means the id comes from the
# script's own cleanup instructions, which cannot name a record it did not make.
$ids = @()
foreach ($line in $create) {
    if ("$line" -match '--delete\s+([0-9a-fA-F-]{36})') { $ids += $Matches[1] }
}
# @() IS LOAD-BEARING. `Select-Object -Unique` returns a bare string when there
# is exactly one match, and indexing a string gives a CHARACTER -- so $ids[0]
# turned a GUID into "c" and the next two steps asked the API for history "c".
# PowerShell collapsing a one-element collection to a scalar is a trap worth
# guarding every time, not just where it has already bitten.
$ids = @($ids | Select-Object -Unique)

if ($ids.Count -eq 0) {
    Write-Host "`nNo history id was produced, so there is nothing to associate." -ForegroundColor Red
    Write-Host "Read the output above - step 1 did not create a record."
    exit 1
}
if ($ids.Count -gt 1) {
    Write-Host "`nMore than one record matched this run, which should not happen." -ForegroundColor Red
    $ids | ForEach-Object { Write-Host "   $_" }
    Write-Host "Refusing to guess which one to modify."
    exit 1
}

$HistoryId = $ids[0]
Write-Host "`nhistory created: $HistoryId" -ForegroundColor Green

Write-Host "`nSTEP 2 - dry run of the association (writes nothing)" -ForegroundColor Cyan
python src/act_write_test.py --user $User --db $Db --associate $HistoryId --contact-id $ContactId

Write-Host "`nSTEP 3 - run it for real" -ForegroundColor Cyan
python src/act_write_test.py --user $User --db $Db --associate $HistoryId --contact-id $ContactId --confirm

Write-Host "`n--------------------------------------------------------------" -ForegroundColor Cyan
Write-Host "NOW LOOK IN ACT!. Open Robert Ladyman's contact, find the history"
Write-Host "entry below, and check which column the contact appears in."
Write-Host ""
Write-Host "   history id : $HistoryId"
Write-Host ""
Write-Host "   ASSOCIATED CONTACT populated -> the endpoint fixes it, and the"
Write-Host "                                   app should call it after logging."
Write-Host "   still only INVITEE           -> the column is not settable and"
Write-Host "                                   the question goes to Act! support."
Write-Host ""
Write-Host "When you are done, remove the test record:"
Write-Host "   python src/act_write_test.py --user $User --db $Db --delete $HistoryId"
