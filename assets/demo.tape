# Leptin demo — generate the README GIF with:  vhs assets/demo.tape
# (install vhs: https://github.com/charmbracelet/vhs)
Output assets/demo.gif

Set FontSize 16
Set Width 1100
Set Height 640
Set Theme "Catppuccin Mocha"
Set Padding 24
Set TypingSpeed 40ms

Type "pip install leptin-mcp"   Sleep 600ms  Enter   Sleep 2s
Type "leptin init"              Sleep 400ms  Enter   Sleep 2s
Type "# remember a few facts, then recall under a token budget" Enter Sleep 800ms
Type "leptin remember 'The backend is FastAPI on Postgres.' --subject stack" Enter Sleep 1500ms
Type "leptin recall 'what is the backend?' --budget 300" Enter Sleep 2s
Type "# show the receipts"      Enter  Sleep 600ms
Type "leptin report"           Sleep 400ms  Enter   Sleep 2500ms
Type "# reproduce the headline, offline"  Enter Sleep 600ms
Type "leptin bench"            Sleep 400ms  Enter   Sleep 3s
Sleep 2s
