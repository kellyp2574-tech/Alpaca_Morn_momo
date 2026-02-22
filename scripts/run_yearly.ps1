param(
    [string]$StartYear = "2021",
    [string]$EndYear = "2026",
    [switch]$Rolling
)

$startY = [int]$StartYear
$endY = [int]$EndYear

New-Item -ItemType Directory -Force -Path ".\reports" | Out-Null

function Run-Year {
    param([int]$year, [string]$start, [string]$end, [string]$label)
    
    $out = ".\reports\$label.txt" -f $year
    Write-Host "=== Running $label ($start -> $end) ===" -ForegroundColor Cyan
    
    $cmd = "python -m backtest.cli pure --slippage 0.005 --opening-strength --exit-time 9:45 --min-volume-5min 1000000 --bucket-range 7-15 --take-profit 0.012 --stop-loss 0.009 --hard-exit 10:30 --portfolio --max-daily-deploy 50000 --start $start --end $end"
    Invoke-Expression $cmd 2>&1 | Out-File -FilePath $out -Encoding utf8
}

function Parse-Summary {
    param([string]$folder)
    
    $files = Get-ChildItem ".\reports\$folder*.txt" -ErrorAction SilentlyContinue
    if (-not $files) { return }
    
    Write-Host "`n=== $folder SUMMARY ===" -ForegroundColor Yellow
    Write-Host "{Year,6} {Start,8} {Final,10} {Ret%,8} {DD%,6} {Trades,7} {AvgRet,8} {AvgDep,10}" -f `
        "Year", "Start", "Final", "Ret%", "DD%", "Trades", "AvgRet", "AvgDep"
    Write-Host ("-" * 65)
    
    foreach ($f in $files) {
        $content = Get-Content $f.FullName -Raw
        if ($content -match "SUMMARY start=(\d+) final=(\d+) ret_pct=([\d.]+) max_dd_pct=([\d.]+) trades=(\d+) avg_ret=([\d.]+) avg_deployed=(\d+)") {
            $year = $f.BaseName -replace "year_", ""
            Write-Host "{0,6} {1,8} {2,10} {3,8} {4,6} {5,7} {6,8} {7,10}" -f `
                $year, $matches[1], $matches[2], $matches[3], $matches[4], $matches[5], $matches[6], $matches[7]
        }
    }
}

# Year-by-year loop
for ($y = $startY; $y -le $endY; $y++) {
    $start = "{0}-01-01" -f $y
    $end = "{0}-12-31" -f $y
    Run-Year -year $y -start $start -end $end -label "year_$y"
}

Parse-Summary -folder "year_"

# Rolling 2-year windows (optional)
if ($Rolling) {
    Write-Host "`n" + ("=" * 50) -ForegroundColor Green
    Write-Host "=== ROLLING 2-YEAR WINDOWS ===" -ForegroundColor Green
    Write-Host ("=" * 50) -ForegroundColor Green
    
    for ($y = $startY; $y -lt $endY; $y++) {
        $y2 = $y + 1
        $start = "{0}-01-01" -f $y
        $end = "{0}-12-31" -f $y2
        Run-Year -year $y -start $start -end $end -label "roll_${y}_${y2}"
    }
    
    Parse-Summary -folder "roll_"
}

Write-Host "`nDone! Reports saved to .\reports\" -ForegroundColor Green
