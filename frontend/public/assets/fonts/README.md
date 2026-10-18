# Interface fonts

The interface serves all fonts locally under `/assets/fonts/`.

- Space Grotesk: headings and Latin UI text.
- Noto Sans SC: Chinese UI text, subset to characters in `frontend/src`.
- IBM Plex Mono: metrics, timestamps and code.

Sources: [Google Fonts](https://fonts.google.com/) and the
[official font repository](https://github.com/google/fonts).
Each font is distributed under the included SIL Open Font License.

Run `python scripts/build_ui_fonts.py` from the repository root after adding
Chinese UI labels to regenerate the subsets. Other characters, including
user-entered data, fall back to the system's CJK sans-serif font.
