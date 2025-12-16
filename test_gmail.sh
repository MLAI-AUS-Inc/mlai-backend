#!/bin/bash
# Usage: ./test_gmail.sh <user_id>
# Defaults to user_id=1 if not provided

USER_ID=${1:-1}
URL="http://localhost:8000/integrations/test/gmail?user_id=$USER_ID"

echo "Testing Gmail fetch for User ID: $USER_ID"
echo "URL: $URL"
echo "----------------------------------------"

curl -s "$URL" | python3 -m json.tool

echo ""
echo "----------------------------------------"
