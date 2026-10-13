#!/usr/bin/env bash
# Wake or sleep the ForgeEval production environment (demo mode; see infra/README.md).
#
#   demo.sh up     [terraform apply args]   create the load balancer and services, wait until /readyz answers
#   demo.sh down   [terraform apply args]   remove the load balancer and services (data stays)
#   demo.sh status                          awake or asleep, running task counts, estimated $/hour
#
# Extra arguments go to `terraform apply` (for example -var-file=/abs/path/prod.tfvars or
# -var database_engine=aurora-serverless). The script runs terraform in infra/envs/prod, so a
# relative -var-file path is relative to that directory. Set DEMO_AUTO_APPROVE=1 to skip the
# confirmation prompt. Needs terraform, the AWS CLI and curl, and AWS credentials in the environment.
set -euo pipefail

usage() {
  sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

[ $# -ge 1 ] || usage
command_name=$1
shift

root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../envs/prod" && pwd)
tf() { terraform -chdir="$root_dir" "$@"; }

# An output that is null (asleep) or missing prints as an empty string.
tf_out() { tf output -raw "$1" 2>/dev/null || true; }

apply_args=()
if [ "${DEMO_AUTO_APPROVE:-0}" = "1" ]; then
  apply_args+=(-auto-approve)
fi

# Prices in USD, us-east-1 (see infra/README.md). The RDS price is unverified.
alb_hour=0.0225
ipv4_hour=0.005
vcpu_hour=0.04048
gb_hour=0.004445
rds_hour=0.016
aurora_acu_hour=0.12
awake_aurora_acu=1 # assumption: average ACUs while awake (the maximum is aurora_max_acu, default 2)

# Estimates use x86 on-demand prices: they overstate ARM64 (about 20% less) and Fargate Spot workers.
# Task sizes are the module defaults (api/worker 0.25 vCPU + 1 GB, web 0.25 vCPU + 0.5 GB).
task_cost() { # vcpu gb count
  awk -v v="$1" -v g="$2" -v n="$3" -v vc="$vcpu_hour" -v gc="$gb_hour" 'BEGIN { printf "%.5f", n * (v * vc + g * gc) }'
}

running_count() { # cluster service
  aws ecs describe-services --cluster "$1" --services "$2" \
    --query 'services[0].runningCount' --output text 2>/dev/null || echo 0
}

wait_until_ready() {
  local cluster dns api worker web
  cluster=$(tf_out ecs_cluster_name)
  dns=$(tf_out alb_dns_name)
  api=$(tf_out api_service_name)
  worker=$(tf_out worker_service_name)
  web=$(tf_out web_service_name)
  if [ -z "$dns" ] || [ -z "$cluster" ] || [ -z "$api" ]; then
    echo "error: terraform has no load balancer or service outputs; is active=false in the state?" >&2
    exit 1
  fi

  echo "Waiting for ECS services to become stable (tasks start, database resumes)..."
  # With console_hosting = s3_cloudfront there is no web service (its output is null, so empty here).
  local services=("$api" "$worker")
  if [ -n "$web" ]; then services+=("$web"); fi
  aws ecs wait services-stable --cluster "$cluster" --services "${services[@]}"

  # The first request after an Aurora pause waits for the database to resume (about 15 s, 30 s or
  # more after a day), so allow generous retries.
  echo "Waiting for http://${dns}/readyz ..."
  curl --fail --silent --show-error --location --retry 20 --retry-delay 6 --retry-all-errors \
    --max-time 20 "http://${dns}/readyz"
  echo
  echo "Awake: http://${dns}"
  local app_url
  app_url=$(tf_out app_url)
  case "$app_url" in
    https://*) echo "Console and API through CloudFront: ${app_url} (the API origin was updated to the new load balancer; CloudFront may need a few minutes to propagate it)" ;;
  esac
}

status() {
  local dns cluster engine api worker web
  dns=$(tf_out alb_dns_name)
  cluster=$(tf_out ecs_cluster_name)
  engine=$(tf_out database_engine)
  engine=${engine:-rds}

  if [ -z "$dns" ]; then
    echo "State: ASLEEP (no load balancer in the Terraform state)"
    echo "Database engine: ${engine}"
    echo "Running tasks: api 0, worker 0, web 0"
    if [ "$engine" = "aurora-serverless" ]; then
      echo "Estimated cost now: about \$0.00/hour for compute; storage, secrets and alarms only (see README cost table)"
    else
      awk -v r="$rds_hour" 'BEGIN { printf "Estimated cost now: about $%.3f/hour (the RDS instance keeps running; only Aurora can pause)\n", r }'
    fi
    return 0
  fi

  api=$(tf_out api_service_name)
  worker=$(tf_out worker_service_name)
  web=$(tf_out web_service_name)
  local api_n worker_n web_n
  api_n=$(running_count "$cluster" "$api")
  worker_n=$(running_count "$cluster" "$worker")
  web_n=0
  if [ -n "$web" ]; then web_n=$(running_count "$cluster" "$web"); fi

  echo "State: AWAKE (load balancer ${dns})"
  echo "Database engine: ${engine}"
  echo "Running tasks: api ${api_n}, worker ${worker_n}, web ${web_n}"

  local tasks fargate ips db total
  tasks=$((api_n + worker_n + web_n))
  fargate=$(awk -v a="$(task_cost 0.25 1 "$api_n")" -v w="$(task_cost 0.25 1 "$worker_n")" -v e="$(task_cost 0.25 0.5 "$web_n")" 'BEGIN { printf "%.5f", a + w + e }')
  # Two load balancer addresses, plus one public IP per task when tasks run in public subnets.
  if [ "$(tf_out assign_public_ip)" = "true" ]; then
    ips=$((2 + tasks))
  else
    ips=2
  fi
  if [ "$engine" = "aurora-serverless" ]; then
    db=$(awk -v a="$awake_aurora_acu" -v p="$aurora_acu_hour" 'BEGIN { printf "%.5f", a * p }')
  else
    db=$rds_hour
  fi
  total=$(awk -v a="$alb_hour" -v i="$ips" -v ih="$ipv4_hour" -v f="$fargate" -v d="$db" 'BEGIN { printf "%.3f", a + i * ih + f + d }')
  echo "Estimated cost now: about \$${total}/hour (load balancer + ${ips} public IPv4 + Fargate ${fargate} + database ${db}; excludes LCUs, logs, data transfer)"
}

case "$command_name" in
  up)
    tf apply -var active=true ${apply_args[@]+"${apply_args[@]}"} "$@"
    wait_until_ready
    ;;
  down)
    tf apply -var active=false ${apply_args[@]+"${apply_args[@]}"} "$@"
    echo "Asleep: load balancer and services removed. Queue, bucket, secrets and database are kept."
    ;;
  status)
    status
    ;;
  *)
    usage
    ;;
esac
