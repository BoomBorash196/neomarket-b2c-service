#!/bin/bash
# Quick check script for B2C service

echo "=== NeoMarket B2C Service - Quick Check ==="
echo ""

# Check Python files syntax
echo "1. Checking Python syntax..."
python3 -m py_compile src/main.py src/config.py src/database.py src/routes/*.py src/models/*.py src/schemas/*.py src/services/*.py src/exceptions.py 2>&1
if [ $? -eq 0 ]; then
    echo "   ✓ All Python files have valid syntax"
else
    echo "   ✗ Syntax errors found"
    exit 1
fi

# Check if required files exist
echo ""
echo "2. Checking required files..."
required_files=(
    "pyproject.toml"
    "docker-compose.yml"
    "Dockerfile"
    "README.md"
    "src/main.py"
    "src/config.py"
    "src/database.py"
    "src/routes/cart.py"
    "src/routes/order.py"
    "src/routes/wishlist.py"
    "src/routes/catalog.py"
    "src/routes/home.py"
    "src/routes/recommendations.py"
    "src/models/__init__.py"
    "src/schemas/__init__.py"
    "migrations/env.py"
    "migrations/versions/001_initial_schema.py"
)

missing=false
for file in "${required_files[@]}"; do
    if [ ! -f "$file" ]; then
        echo "   ✗ Missing: $file"
        missing=true
    fi
done

if [ "$missing" = false ]; then
    echo "   ✓ All required files present"
fi

# Check tests
echo ""
echo "3. Checking tests..."
if [ -d "tests" ] && [ "$(ls -A tests/*.py 2>/dev/null)" ]; then
    echo "   ✓ Tests directory exists with test files"
else
    echo "   ⚠ No tests found (optional for M1)"
fi

echo ""
echo "=== Check Complete ==="
echo ""
echo "Next steps:"
echo "  1. git init && git add . && git commit -m 'Initial B2C service'"
echo "  2. Create GitHub repo and push: git remote add origin <url> && git push"
echo "  3. Install deps: pip install -e '.[dev]'"
echo "  4. Run tests: pytest tests/ -v"
echo "  5. When B2B is ready: docker compose up --build"
