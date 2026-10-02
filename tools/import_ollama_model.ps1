# Copy a model downloaded with tools/fetch_ollama_model.py into the Ollama model volume.
# Local copy only: the runtime `ollama` container never needs internet access (SEC-9).
# Uses `docker cp` (Docker API stream) rather than a bind mount: on Docker Desktop for Windows
# large files read through the bind-mount share failed with I/O errors.
#   powershell -File tools/import_ollama_model.ps1 [-Source data\ollama_models]
param(
    [string]$Source = "data\ollama_models",
    [string]$Volume = "agentic-ai-trading-system_ollama-models",
    [string]$Image = "ollama/ollama:latest"
)
$ErrorActionPreference = "Stop"
$src = (Resolve-Path $Source).Path
if (-not (Test-Path (Join-Path $src "manifests"))) { throw "no manifests in ${src}: run tools/fetch_ollama_model.py first" }
if (Get-ChildItem (Join-Path $src "blobs") -Filter *.part -ErrorAction SilentlyContinue) { throw "unfinished download (*.part) in ${src}: rerun the fetch script" }

docker volume create $Volume | Out-Null
$helper = "trader-model-import"
docker rm -f $helper 2>$null | Out-Null
docker create --name $helper --network none --entrypoint sleep -v "${Volume}:/root/.ollama" $Image infinity | Out-Null
try {
    docker start $helper | Out-Null
    docker exec $helper sh -c "mkdir -p /root/.ollama/models/blobs /root/.ollama/models/manifests && rm -f /root/.ollama/models/blobs/*-partial*"
    foreach ($f in Get-ChildItem (Join-Path $src "blobs") -File) {
        Write-Host ("copying {0} ({1:N2} GB)" -f $f.Name.Substring(0, 19), ($f.Length / 1GB))
        docker cp $f.FullName "${helper}:/root/.ollama/models/blobs/$($f.Name)"
        if ($LASTEXITCODE -ne 0) { throw "docker cp failed for $($f.Name)" }
        $sum = (docker exec $helper sha256sum "/root/.ollama/models/blobs/$($f.Name)").Split(" ")[0]
        if ("sha256-$sum" -ne $f.Name) { throw "checksum mismatch after copy: $($f.Name)" }
    }
    docker cp (Join-Path $src "manifests\.") "${helper}:/root/.ollama/models/manifests/"
    if ($LASTEXITCODE -ne 0) { throw "docker cp failed for manifests" }
}
finally {
    docker rm -f $helper | Out-Null
}

# Ollama reads the volume at start-up: restart it and list the models it can see
docker compose -f docker/docker-compose.yml restart ollama | Out-Null
Start-Sleep -Seconds 5
docker compose -f docker/docker-compose.yml exec ollama ollama list
