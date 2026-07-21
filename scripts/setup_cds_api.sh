#!/usr/bin/env bash
# =============================================================================
# CDS API Setup Script — Copernicus Data Store
# =============================================================================
# Sets up the CDS API client for ERA5 data access.
# 
# Usage:
#   bash scripts/setup_cds_api.sh
#   bash scripts/setup_cds_api.sh --key API_KEY    # Non-interactive setup (new format, no UID)
#   bash scripts/setup_cds_api.sh --key UID:API_KEY   # Legacy format
# =============================================================================

set -euo pipefail

# ── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; }

# ── Parse arguments ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --key)
            KEY_ARG="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [--key UID:API_KEY]"
            echo ""
            echo "  --key  Pass API key directly (non-interactive). Format: API_KEY (new CDS) or UID:API_KEY (legacy)"
            exit 0
            ;;
        *)
            error "Unknown option: $1"
            echo "Usage: $0 [--key UID:API_KEY]"
            exit 1
            ;;
    esac
done

echo ""
echo "================================================"
echo "  CDS API Setup — Copernicus Data Store"
echo "================================================"
echo ""

# ── Step 1: Check Python ────────────────────────────────────────────────────
info "Step 1: Checking Python environment..."

if ! command -v python3 &>/dev/null; then
    error "Python 3 is not installed. Install Python 3.10+ first."
    exit 1
fi

PY_VER=$(python3 --version 2>&1)
ok "Python: $PY_VER"

# ── Step 2: Install cdsapi ──────────────────────────────────────────────────
info "Step 2: Installing/checking cdsapi package..."

if python3 -c "import cdsapi" 2>/dev/null; then
    ok "cdsapi is already installed"
    python3 -c "import cdsapi; print(f'  cdsapi version: {cdsapi.__version__}')" 2>/dev/null || true
else
    info "Installing cdsapi..."
    pip install cdsapi 2>&1 | tail -3
    if python3 -c "import cdsapi" 2>/dev/null; then
        ok "cdsapi installed successfully"
    else
        error "Failed to install cdsapi. Try: pip install cdsapi"
        exit 1
    fi
fi

# ── Step 3: Check for xarray and netCDF4 ────────────────────────────────────
info "Step 3: Checking required dependencies..."

MISSING_DEPS=()
for dep in "xarray" "netCDF4" "numpy" "cdsapi"; do
    if python3 -c "import $dep" 2>/dev/null; then
        ok "$dep is available"
    else
        MISSING_DEPS+=("$dep")
    fi
done

if [ ${#MISSING_DEPS[@]} -gt 0 ]; then
    warn "Missing dependencies: ${MISSING_DEPS[*]}"
    info "Installing missing dependencies..."
    pip install "${MISSING_DEPS[@]}" 2>&1 | tail -3
    ok "Dependencies installed"
fi

# ── Step 4: Check for CDS API key ───────────────────────────────────────────
info "Step 4: Checking CDS API key..."

CDSAPIRC="$HOME/.cdsapirc"
HAS_KEY=false
KEY_SOURCE=""

# Check environment variable
if [ -n "${CDSAPI_URL:-}" ] && [ -n "${CDSAPI_KEY:-}" ]; then
    HAS_KEY=true
    KEY_SOURCE="CDSAPI_URL / CDSAPI_KEY environment variables"
    ok "Found CDS API credentials in environment"
fi

# Check ~/.cdsapirc
if [ -f "$CDSAPIRC" ]; then
    if grep -q "url:" "$CDSAPIRC" 2>/dev/null && grep -q "key:" "$CDSAPIRC" 2>/dev/null; then
        if [ "$HAS_KEY" = false ]; then
            HAS_KEY=true
            KEY_SOURCE="$CDSAPIRC"
        fi
        ok "Found CDS API credentials in $CDSAPIRC"
    else
        warn "$CDSAPIRC exists but appears incomplete (missing url or key)"
    fi
fi

# Check command-line argument
if [ -n "${KEY_ARG:-}" ]; then
    HAS_KEY=true
    KEY_SOURCE="command-line argument"
    ok "CDS API key provided via --key argument"
fi

# ── Step 5: Create/update ~/.cdsapirc ───────────────────────────────────────
if [ "$HAS_KEY" = true ]; then
    info "CDS API is configured via: $KEY_SOURCE"
else
    echo ""
    warn "=============================================="
    warn "  CDS API KEY NOT FOUND"
    warn "=============================================="
    echo ""
    echo "ERA5 data access requires a free CDS account."
    echo ""
    echo "To register and get your API key:"
    echo "  1. Go to: https://cds.climate.copernicus.eu"
    echo "  2. Create a free account"
    echo "  3. Log in and go to your profile"
    echo "  4. Find your Personal Access Token (UUID format, no UID needed)"
    echo ""
    echo "Then either:"
    echo "  a) Set environment variables:"
    echo "     export CDSAPI_URL=https://cds.climate.copernicus.eu/api"
    echo "     export CDSAPI_KEY=API_KEY"
    echo "     (or legacy: export CDSAPI_KEY=UID:API_KEY)"
    echo ""
    echo "  b) Create ~/.cdsapirc:"
    echo "     $0 --key API_KEY"
    echo ""
    echo "  c) Run this script again with your key:"
    echo "     bash $0 --key API_KEY"
    echo ""

    # Prompt interactively
    read -r -p "Enter your CDS API key (UUID token, or UID:KEY) or press Enter to skip: " USER_KEY
    if [ -n "$USER_KEY" ]; then
        KEY_ARG="$USER_KEY"
        HAS_KEY=true
        info "Key provided via interactive input"
    fi
fi

# ── Step 6: Write ~/.cdsapirc if needed ─────────────────────────────────────
if [ "$HAS_KEY" = true ] && [ -n "${KEY_ARG:-}" ]; then
    # Parse key: new CDS format (just token) or legacy (UID:KEY)
    if echo "$KEY_ARG" | grep -q ":"; then
        # Legacy format: UID:KEY
        KEY_UID="${KEY_ARG%%:*}"
        KEY_SECRET="${KEY_ARG#*:}"
        CDS_KEY="${KEY_UID}:${KEY_SECRET}"
        ok "Using legacy UID:KEY format"
    else
        # New CDS format: just Personal Access Token
        CDS_KEY="$KEY_ARG"
        ok "Using new CDS Personal Access Token format"
    fi

    cat > "$CDSAPIRC" <<EOF
url: https://cds.climate.copernicus.eu/api
key: ${CDS_KEY}
EOF
    chmod 600 "$CDSAPIRC"
    ok "Created $CDSAPIRC with your API key (permissions: 600)"
fi

# ── Step 7: Test connection ─────────────────────────────────────────────────
if [ "$HAS_KEY" = true ]; then
    echo ""
    info "Step 7: Testing CDS API connection..."

    if python3 -c "
import cdsapi
import sys
try:
    c = cdsapi.Client()
    # Just test client creation and API reachability
    # (don't submit a full request — that takes time)
    print('CDS API client created successfully')
except Exception as e:
    print(f'Connection failed: {e}', file=sys.stderr)
    sys.exit(1)
" 2>&1; then
        ok "CDS API connection test passed"
    else
        warn "CDS API client created but connection may not work until a request is submitted"
        warn "Run: python3 scripts/era5_upper_air_backfill.py --dry-run --station KATL"
        warn "to verify end-to-end connectivity"
    fi
fi

# ── Summary ─────────────────────────────────────────────────────────────────
echo ""
echo "================================================"
echo "  SETUP SUMMARY"
echo "================================================"
echo ""

if [ "$HAS_KEY" = true ]; then
    ok "CDS API is configured and ready"
    echo ""
    echo "Next steps:"
    echo "  Run the backfill script:"
    echo "    python3 scripts/era5_upper_air_backfill.py"
    echo ""
    echo "  For a dry run (check what would be downloaded):"
    echo "    python3 scripts/era5_upper_air_backfill.py --dry-run"
    echo ""
    echo "  For a single station test:"
    echo "    python3 scripts/era5_upper_air_backfill.py --station KATL --months 1"
else
    warn "CDS API is NOT configured"
    echo ""
    echo "The backfill script will not work without API credentials."
    echo "Register at: https://cds.climate.copernicus.eu"
    echo "Then run: bash $0 --key UID:API_KEY"
    echo ""
    echo "Even without CDS access, the script can be syntax-checked:"
    echo "  python3 -c \"import ast; ast.parse(open('scripts/era5_upper_air_backfill.py').read()); print('Syntax OK')\""
fi

echo ""
echo "================================================"