#!/bin/bash
# GitHub setup script for porsche-tracker
# Run this once with your GitHub Personal Access Token
# Usage: bash github_setup.sh YOUR_GITHUB_TOKEN

TOKEN="${1}"
GITHUB_USER="OCX11"
REPO_NAME="porsche-tracker"

if [ -z "$TOKEN" ]; then
  echo "Usage: bash github_setup.sh YOUR_GITHUB_TOKEN"
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Working in: $REPO_DIR"

# 1. Create the private repo via GitHub API
echo "Creating private GitHub repo..."
RESULT=$(curl -s -X POST \
  -H "Authorization: token $TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/user/repos \
  -d "{\"name\":\"$REPO_NAME\",\"private\":true,\"auto_init\":false,\"description\":\"Porsche tracker dashboard\"}")

CLONE_URL=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('clone_url',''))" 2>/dev/null)
HTML_URL=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('html_url',''))" 2>/dev/null)

if [ -z "$CLONE_URL" ]; then
  echo "Repo may already exist or creation failed. Trying to use existing..."
  CLONE_URL="https://github.com/$GITHUB_USER/$REPO_NAME.git"
  HTML_URL="https://github.com/$GITHUB_USER/$REPO_NAME"
else
  echo "Repo created: $HTML_URL"
fi

# 2. Initialize git in the project directory
cd "$REPO_DIR"
git init
git config user.email "openclawx1@protonmail.com"
git config user.name "OCX11"

# 3. Add remote (with token embedded for auth)
AUTH_URL="https://$GITHUB_USER:$TOKEN@github.com/$GITHUB_USER/$REPO_NAME.git"
git remote remove origin 2>/dev/null || true
git remote add origin "$AUTH_URL"

# 4. Stage and commit everything
git add .gitignore scraper.py db.py main.py new_dashboard.py requirements.txt
git add static/index.html static/dashboard.html 2>/dev/null || true
git commit -m "Initial commit: Porsche tracker dashboard with Rennlist, BaT, PCA, dealers"

# 5. Push to GitHub
git branch -M main
git push -u origin main

echo ""
echo "Done! Repo is live at: $HTML_URL"
echo ""
echo "Next: Enable GitHub Pages at:"
echo "  $HTML_URL/settings/pages"
echo "  Source: Deploy from branch 'main', folder '/static'"
