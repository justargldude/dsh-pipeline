#!/bin/bash
# ==============================================================================
# Auto-Installer for dsh-joi-channel-theme (Joi Flowers & Joi Library Dual Theme)
# Works on any machine running DeepSeek Harness (DSH)
# ==============================================================================

set -e

echo "=========================================================="
echo "🌸 Installing Joi Dual-Outfit Theme (dsh-joi-channel-theme)..."
echo "=========================================================="

DSH_PROFILE_DIR="$HOME/.dsh/profiles/web"
PATCH_FILE="$DSH_PROFILE_DIR/cordis.patch.yml"

mkdir -p "$DSH_PROFILE_DIR"

# 1. Install npm package inside web profile
if command -v pnpm >/dev/null 2>&1; then
    echo "📦 Installing dsh-joi-channel-theme via pnpm..."
    cd "$DSH_PROFILE_DIR"
    pnpm add dsh-joi-channel-theme
elif command -v npm >/dev/null 2>&1; then
    echo "📦 Installing dsh-joi-channel-theme via npm..."
    cd "$DSH_PROFILE_DIR"
    npm install dsh-joi-channel-theme
else
    echo "❌ Error: Neither pnpm nor npm was found on PATH."
    exit 1
fi

# 2. Register plugin in cordis.patch.yml
if [ -f "$PATCH_FILE" ]; then
    if ! grep -q "dsh-joi-channel-theme" "$PATCH_FILE"; then
        echo "📝 Registering dsh-joi-channel-theme in cordis.patch.yml..."
        cat << EOP >> "$PATCH_FILE"

- insert:
    - id: joi-channel-theme
      name: dsh-joi-channel-theme
EOP
    else
        echo "ℹ️ Plugin is already registered in cordis.patch.yml."
    fi
else
    echo "📝 Creating cordis.patch.yml..."
    cat << EOP > "$PATCH_FILE"
- insert:
    - id: joi-channel-theme
      name: dsh-joi-channel-theme
EOP
fi

# 3. English localization & High Contrast Slash Command Theme Patch
THEME_CLIENT="$DSH_PROFILE_DIR/node_modules/dsh-joi-channel-theme/lib/client.js"
if [ -f "$THEME_CLIENT" ]; then
    python3 -c "
with open('$THEME_CLIENT', 'r', encoding='utf-8') as f:
    c = f.read()
c = c.replace('children: \"换装\"', 'children: \"Wardrobe & Themes\"')
c = c.replace('children: \"选一套衣装，房间会跟着换；也可以回到 DeepSeek 原生外观\"', 'children: \"Choose an outfit theme (Joi-Flowers / Joi-Library), or revert to DeepSeek Native\"')

slash_patch = '''
/* ── Slash Commands Menu & System Cards Enhancements (High Contrast Purple-Pink Glow) ── */
[role=listbox], [role=menu], [data-radix-popper-content-wrapper],
[class*=popover], [class*=dropdown], [class*=suggestion], [class*=slashMenu], [class*=commandMenu], [class*=_menu] {
  background: rgba(24, 18, 38, 0.96) !important;
  border: 1px solid rgba(192, 132, 252, 0.6) !important;
  box-shadow: 0 10px 30px rgba(168, 85, 247, 0.35) !important;
  border-radius: 10px !important;
  backdrop-filter: blur(16px) !important;
  color: #ffffff !important;
}
[role=option], [role=menuitem], [class*=item], [class*=commandItem], [class*=suggestionItem], [class*=_menuItem] {
  color: #ffffff !important;
  border-radius: 6px !important;
  margin: 2px 4px !important;
}
[role=option] span, [role=menuitem] span, [class*=item] span, [class*=commandItem] span { color: #ffffff !important; }
[role=option]:hover, [role=option][aria-selected=true], [role=menuitem]:hover, [role=menuitem][aria-selected=true],
[class*=item]:hover, [class*=item][class*=selected], [class*=commandItem]:hover {
  background: linear-gradient(90deg, rgba(168, 85, 247, 0.45) 0%, rgba(244, 114, 182, 0.35) 100%) !important;
  color: #ffffff !important;
  box-shadow: inset 3px 0 0 #f472b6, 0 0 12px rgba(244, 114, 182, 0.3) !important;
}
[class*=commandName], [class*=commandPrefix], [class*=slashCommand], [role=option] [class*=title], [role=option] strong {
  color: #f472b6 !important;
  font-weight: 600 !important;
  text-shadow: 0 0 8px rgba(244, 114, 182, 0.4) !important;
}
[data-disclosure-row], [class*=disclosureRow], [class*=disclosureCard], [class*=commandCard], [data-sample] {
  background: rgba(28, 22, 44, 0.88) !important;
  border: 1px solid rgba(192, 132, 252, 0.4) !important;
  border-radius: 8px !important;
  color: #ffffff !important;
}
[data-disclosure-row] summary, [data-disclosure-row] header, [data-disclosure-row] [class*=header], [data-disclosure-row] [class*=title] {
  color: #fdf4ff !important;
}
[data-disclosure-row] [class*=badge], [data-disclosure-row] [class*=tag], [data-disclosure-row] [class*=label] {
  color: #c084fc !important;
  background: rgba(168, 85, 247, 0.2) !important;
  border: 1px solid rgba(168, 85, 247, 0.4) !important;
  border-radius: 4px !important;
}
[data-disclosure-row] pre, [data-disclosure-row] code, [data-disclosure-row] [class*=content], [data-disclosure-row] [class*=body] {
  color: #ffffff !important;
  background: rgba(16, 12, 26, 0.95) !important;
  border: 1px solid rgba(192, 132, 252, 0.25) !important;
}
'''
if 'Slash Commands Menu & System Cards Enhancements' not in c:
    target_marker = \"[class*=Markdown] > ul > li::marker { content: '🍊 '; }\"
    if target_marker in c:
        c = c.replace(target_marker, target_marker + '\n' + slash_patch)

with open('$THEME_CLIENT', 'w', encoding='utf-8') as f:
    f.write(c)
" 2>/dev/null || true
fi

echo "=========================================================="
echo "✅ Installation complete!"
echo "👉 Restart DSH ('dsh web' or restart your service) and refresh browser."
echo "👉 Open Settings ⚙️ ➡️ General ➡️ Wardrobe to select Joi-Flowers or Joi-Library!"
echo "=========================================================="
