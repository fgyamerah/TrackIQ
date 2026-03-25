This bundle is meant to be handed to Claude Code so it can integrate the feature into your existing DJ Toolkit.

Included:
- label_intel package
- requirements file
- seed example
- implementation prompt for Claude

Recommended workflow:
1. Drop these files into the root of your toolkit repo.
2. Give Claude the prompt from `CLAUDE_IMPLEMENTATION_PROMPT.txt`.
3. Ask Claude to patch your actual CLI / pipeline / config files.
4. Test with:
   python -m label_intel.cli scrape --seeds seeds_example.txt --out-dir output/labels

Outputs:
- labels.json
- labels.csv
- labels.txt
- labels.sqlite
