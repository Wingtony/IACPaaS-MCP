param([int]$Port = 11435, [string]$Models = 'D:\LocalLLM\models')
$ErrorActionPreference = 'Stop'
$endpoint = "http://127.0.0.1:$Port"
try {
    $tags = Invoke-RestMethod "$endpoint/api/tags" -TimeoutSec 3
    if (-not ($tags.models.name -contains 'qwen-agent:latest')) { throw "На $endpoint нет qwen-agent" }
    Write-Host "Qwen готов: $endpoint"
    exit 0
} catch {
    try { Invoke-RestMethod "$endpoint/api/version" -TimeoutSec 2 | Out-Null; throw "Порт $Port занят другим Ollama. Укажите другой -Port и OLLAMA_URL в .env" }
    catch { if ($_.Exception.Message -like 'Порт*') { throw } }
}
if (-not (Test-Path -LiteralPath $Models)) { throw "Нет каталога весов: $Models" }
$ollamaExe = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
$env:OLLAMA_HOST = "127.0.0.1:$Port"
$env:OLLAMA_MODELS = $Models
$env:OLLAMA_CONTEXT_LENGTH = '32768'
$env:OLLAMA_NUM_PARALLEL = '1'
$logDir = Join-Path $PSScriptRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Start-Process -FilePath $ollamaExe -ArgumentList 'serve' -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'ollama.stdout.log') -RedirectStandardError (Join-Path $logDir 'ollama.stderr.log')
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Seconds 1
    try {
        $tags = Invoke-RestMethod "$endpoint/api/tags" -TimeoutSec 2
        if (-not ($tags.models.name -contains 'qwen-agent:latest')) { throw 'Модель qwen-agent отсутствует' }
        Write-Host "Qwen готов: $endpoint"
        exit 0
    } catch { }
}
throw 'Ollama не запустился или не нашёл qwen-agent. Проверьте logs/ollama.stderr.log'
