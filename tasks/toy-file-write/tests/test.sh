#!/bin/bash
# Reward 1 when /app/hello.txt holds exactly "hello from the agent\n".
mkdir -p /logs/verifier
expected="hello from the agent"
if [ -f /app/hello.txt ] && [ "$(cat /app/hello.txt)" = "$expected" ] \
   && [ "$(wc -c < /app/hello.txt | tr -d ' ')" = "21" ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo "hello.txt missing or wrong:"; ls -la /app; cat /app/hello.txt 2>/dev/null | od -c | head
  echo 0 > /logs/verifier/reward.txt
fi
