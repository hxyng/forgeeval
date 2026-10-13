# Used only by the Trivy scan in .github/workflows/infra.yml: switches every optional feature on so
# resources behind `count` (CloudFront console, Aurora, Spot capacity, step scaling) are scanned too.
# Contains no secrets; deliberately committed (see infra/.gitignore).
console_hosting    = "s3_cloudfront"
cpu_architecture   = "ARM64"
worker_capacity    = "FARGATE_SPOT"
worker_min_count   = 0
container_insights = false
database_engine    = "aurora-serverless"
enable_nat_gateway = true
