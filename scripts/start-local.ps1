$ErrorActionPreference = "Stop"

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$gitSha = (git -C $repositoryRoot rev-parse HEAD).Trim().ToLowerInvariant()
if ($gitSha -notmatch '^[0-9a-f]{40}$') {
    throw "无法解析当前 Git SHA，已停止构建"
}

$env:PAPERLEAF_GIT_SHA = $gitSha
docker compose -f (Join-Path $repositoryRoot "compose.yaml") up -d --build @args
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose 启动失败"
}

$containerSha = (
    docker compose -f (Join-Path $repositoryRoot "compose.yaml") exec -T api `
        python -c "import os; print(os.environ.get('PAPERLEAF_GIT_SHA', ''))"
).Trim().ToLowerInvariant()
if ($LASTEXITCODE -ne 0 -or $containerSha -ne $gitSha) {
    throw "容器 Git SHA 校验失败：期望 $gitSha，实际 $containerSha"
}

Write-Output "PaperLeaf 已使用并验证 Git SHA $gitSha 启动"
