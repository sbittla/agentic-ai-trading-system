# Copy a model downloaded with tools/fetch_ollama_model.py into the Ollama model volume.
# Local copy only: the runtime `ollama` container never needs internet access (SEC-9).
# Uses `docker cp` (Docker API stream) rather than a bind mount: on Docker Desktop for Windows
# large files read through the bind-mount share failed with I/O errors.
# Large blobs go in chunks (default 256 MB): one multi-GB `docker cp` crashed Docker Desktop
# when Windows was short of commit memory, and a crash now loses one chunk, not the copy.
# Rerunning resumes from the bytes already in the volume; every blob is sha256-verified.
#   powershell -File tools/import_ollama_model.ps1 [-Source data\ollama_models] [-ChunkMB 256]
param(
    [string]$Source = "data\ollama_models",
    [string]$Volume = "agentic-ai-trading-system_ollama-models",
    [string]$Image = "ollama/ollama:latest",
    [int]$ChunkMB = 256
)
$ErrorActionPreference = "Stop"
$src = (Resolve-Path $Source).Path
if (-not (Test-Path (Join-Path $src "manifests"))) { throw "no manifests in ${src}: run tools/fetch_ollama_model.py first" }
if (Get-ChildItem (Join-Path $src "blobs") -Filter *.part -ErrorAction SilentlyContinue) { throw "unfinished download (*.part) in ${src}: rerun the fetch script" }

function Invoke-Docker {
    docker @args
    if ($LASTEXITCODE -ne 0) { throw "docker $($args -join ' ') failed (exit $LASTEXITCODE)" }
}

docker volume create $Volume | Out-Null
$helper = "trader-model-import"
docker rm -f $helper 2>$null | Out-Null
Invoke-Docker create --name $helper --network none --entrypoint sleep -v "${Volume}:/root/.ollama" $Image infinity | Out-Null
$chunkBytes = [int64]$ChunkMB * 1MB
$tmp = Join-Path ([IO.Path]::GetTempPath()) "ollama-import-chunk.bin"
try {
    Invoke-Docker start $helper | Out-Null
    Invoke-Docker exec $helper sh -c "mkdir -p /root/.ollama/models/blobs /root/.ollama/models/manifests && rm -f /root/.ollama/models/blobs/*-partial* /root/.ollama/models/blobs/*.chunk"
    foreach ($f in Get-ChildItem (Join-Path $src "blobs") -File) {
        $dest = "/root/.ollama/models/blobs/$($f.Name)"
        $have = [int64](docker exec $helper sh -c "stat -c %s $dest 2>/dev/null || echo 0")
        if ($have -eq $f.Length) {
            $sum = (docker exec $helper sha256sum $dest).Split(" ")[0]
            if ("sha256-$sum" -eq $f.Name) { Write-Host "ok      $($f.Name.Substring(0, 19)) (already in volume)"; continue }
            $have = 0
        }
        if ($have -gt $f.Length -or ($have % $chunkBytes) -ne 0) { $have = 0 }   # resume only on a chunk boundary
        if ($have -eq 0) { Invoke-Docker exec $helper sh -c ": > $dest" }
        Write-Host ("copying {0} ({1:N2} GB) from {2:N2} GB" -f $f.Name.Substring(0, 19), ($f.Length / 1GB), ($have / 1GB))
        $in = [IO.File]::OpenRead($f.FullName)
        try {
            $in.Position = $have
            $buf = New-Object byte[] ([Math]::Min($chunkBytes, [Math]::Max($f.Length, 1)))
            while ($in.Position -lt $f.Length) {
                $n = 0
                while ($n -lt $buf.Length) {
                    $r = $in.Read($buf, $n, $buf.Length - $n)
                    if ($r -le 0) { break }
                    $n += $r
                }
                $out = [IO.File]::Create($tmp)
                try { $out.Write($buf, 0, $n) } finally { $out.Dispose() }
                Invoke-Docker cp $tmp "${helper}:$dest.chunk"
                Invoke-Docker exec $helper sh -c "cat $dest.chunk >> $dest && rm $dest.chunk"
                Write-Host ("  {0:N2} / {1:N2} GB" -f ($in.Position / 1GB), ($f.Length / 1GB))
            }
        }
        finally { $in.Dispose() }
        $sum = (docker exec $helper sha256sum $dest).Split(" ")[0]
        if ("sha256-$sum" -ne $f.Name) {
            docker exec $helper rm -f $dest | Out-Null
            throw "checksum mismatch after copy: $($f.Name) (removed; rerun the script)"
        }
    }
    Invoke-Docker cp (Join-Path $src "manifests\.") "${helper}:/root/.ollama/models/manifests/"
}
finally {
    Remove-Item $tmp -ErrorAction SilentlyContinue
    docker rm -f $helper | Out-Null
}

# Ollama reads the volume at start-up: restart it and list the models it can see
docker compose -f docker/docker-compose.yml restart ollama | Out-Null
Start-Sleep -Seconds 5
docker compose -f docker/docker-compose.yml exec ollama ollama list
