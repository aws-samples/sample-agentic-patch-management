#!/bin/bash
# Break httpd on 3 staging instances for demo

INSTANCES=$(aws ec2 describe-instances \
  --profile ri25-demo \
  --region us-east-1 \
  --filters "Name=tag:Environment,Values=staging" "Name=instance-state-name,Values=running" \
  --query 'Reservations[*].Instances[*].InstanceId' \
  --output text | head -3)

aws ssm send-command \
  --profile ri25-demo \
  --region us-east-1 \
  --instance-ids $INSTANCES \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["systemctl stop httpd"]'

echo "Stopped httpd on: $INSTANCES"
