import re

with open('src/ledgerhand/browser/skills.py', 'r') as f:
    text = f.read()

text = re.sub(
    r'page\.goto\(f"\{base\}(.*?)"\)\s*_check_session\(page, base, config\)',
    r'_check_session(page, base, config, "\1")',
    text
)
# Special case for bills?vendor={vendor_id}&page={pg}
text = re.sub(
    r'page\.goto\(f"\{base\}/bills\?vendor=\{vendor_id\}&page=\{pg\}"\)\s*_check_session\(page, base, config\)',
    r'_check_session(page, base, config, f"/bills?vendor={vendor_id}&page={pg}")',
    text
)

with open('src/ledgerhand/browser/skills.py', 'w') as f:
    f.write(text)
