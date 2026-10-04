import re

with open('src/ledgerhand/browser/skills.py', 'r') as f:
    text = f.read()

text = re.sub(
    r'_check_session\(page, base, config, "(.*?)"\)',
    r'_check_session(page, base, config, f"\1")',
    text
)

with open('src/ledgerhand/browser/skills.py', 'w') as f:
    f.write(text)
