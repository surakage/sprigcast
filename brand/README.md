# SprigCast brand

Vector files, safe to scale to any size (print, embroidery, web).

| File | Use |
| --- | --- |
| `sprigcast-s-color.svg` | Main logo: the sprig S, emerald `#2a9c5e` with the pink live dot `#ff5c8a`. Light backgrounds. |
| `sprigcast-s-white-pink.svg` | The S in white with the pink dot, for dark backgrounds. |
| `sprigcast-s-white.svg` | All white, for dark shirts and one-colour prints. |
| `sprigcast-s-onecolor.svg` | All dark green `#1d6b43`, for embroidery and stamps. |
| `sprigcast-app-icon.svg` | App icon: mint S on a dark green tile (same as `src/sprigcast/data/logo.svg`). |
| `sprigcast-sloth.svg` | Mascot, full colour. |
| `sprigcast-sloth-white.svg` | Mascot in one colour, white, for dark shirts (the face shows the shirt through). |
| `sprigcast-sloth-onecolor.svg` | Mascot in one colour, dark green. |
| `sprigcast-banner.png` | 1280 x 640 banner for the README and GitHub's social preview (Settings → General → Social preview). |

The app's own icons (`.ico` for the installer and shortcuts) are made from `src/sprigcast/data/logo.svg` by `packaging/make_icon.py`. The installer's brand art (the tall Welcome/Finish picture and the small header icon) is drawn from these files by `packaging/make_wizard_images.py` at build time.
