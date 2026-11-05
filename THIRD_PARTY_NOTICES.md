# Third-party materials

This register identifies the approved implementation components and the exact
local dependency versions selected under the current product plan. A listing is
not a claim that its source, package, font, model or binary is present in a given
GitHub tree, nor that its product behavior, packaging or release has passed.
The repository manifest and actual tree determine the stage; local unpublished
implementation and runtime assets are accounted for separately. These components
retain their own copyright and licenses. This register grants no rights to
materials in Git history.

| Component | Bound version | License / source |
| --- | --- | --- |
| Flask | 3.1.3 | BSD-3-Clause — https://github.com/pallets/flask |
| Waitress | 3.0.2 | ZPL-2.1 — https://github.com/Pylons/waitress |
| python-docx | 1.2.0 | MIT — https://github.com/python-openxml/python-docx |
| lxml | 6.1.3 | BSD-3-Clause with the distribution's LICENSES.txt exceptions and bundled-library notices — https://github.com/lxml/lxml |
| pdfplumber | 0.11.10 | MIT — https://github.com/jsvine/pdfplumber |
| pypdfium2 and bundled Windows PDFium | pypdfium2 5.14.0 (`py3-none-win_amd64`); bundled PDFium 156.0.8076.0, origin `pdfium-binaries` | Apache-2.0 / BSD-3-Clause for pypdfium2 code; CC-BY-4.0 for its documentation/examples; PDFium and bundled libraries retain the actual wheel's component licenses — https://github.com/pypdfium2-team/pypdfium2 |
| Playwright Python package and bundled driver | 1.64.0 | Apache-2.0 for Playwright; retain Microsoft/Puppeteer attribution and the bundled Node.js and third-party notices — https://github.com/microsoft/playwright-python |
| python-pptx | 1.0.2 | MIT — https://github.com/scanny/python-pptx |
| XlsxWriter | 3.2.9 | BSD-2-Clause — https://github.com/jmcnamara/XlsxWriter |
| openpyxl | 3.1.5 | MIT — https://foss.heptapod.net/openpyxl/openpyxl |
| et-xmlfile | 2.0.0 | MIT — https://foss.heptapod.net/openpyxl/et_xmlfile |
| OpenAI Whisper code | 20250625 (`31243bad24cc746f07d4c8bfdd2d974872cb1803`) | MIT — https://github.com/openai/whisper/blob/v20250625/LICENSE |
| OpenAI Whisper `base` model weights | official base checkpoint; SHA-256 `ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e` | MIT, per OpenAI Whisper model notice — https://github.com/openai/whisper#license |
| PyTorch CPU runtime | 2.14.1+cpu, CPython 3.13.15 Windows x64 wheel | BSD-style and the wheel's bundled license files — https://github.com/pytorch/pytorch/blob/v2.14.1/LICENSE ; official CPU wheel index: https://download.pytorch.org/whl/cpu |
| Flite via the local FFmpeg filter | Gyan FFmpeg 9.0.2 `full_build`, compiled with `--enable-libflite`; linked Flite library version NOT_REPORTED | The binary README identifies the build as GPL v3. Flite's upstream code/voice license is BSD-like; retain the exact FFmpeg build notices and Flite notices when redistributing — https://ffmpeg.org/ffmpeg-filters.html#flite ; https://github.com/festvox/flite/blob/v2.2/COPYING |
| eSpeak NG engine and speech data | 1.52.0 source tag, commit `4870adfa25b1a32b4361592f1be8a40337c58d6c`; source archive SHA-256 `b4517592e3cbc43703bb1c782702eafb98097659e0b7e96e69c32ee32ae5003a` | GPL-3.0-or-later core/data plus bundled UCD, BSD-2-Clause and Apache notices in the source package; the local runtime preserves `COPYING`, `COPYING.UCD`, `COPYING.BSD2`, `COPYING.APACHE` and `src/ucd-tools` notices — https://github.com/espeak-ng/espeak-ng/blob/1.52.0/COPYING ; https://github.com/espeak-ng/espeak-ng/blob/1.52.0/COPYING.UCD ; https://github.com/espeak-ng/espeak-ng/blob/1.52.0/COPYING.BSD2 ; https://github.com/espeak-ng/espeak-ng/blob/1.52.0/COPYING.APACHE |
| ReportLab | 5.0.1 | BSD license from the pinned distribution — https://www.reportlab.com/opensource/ |
| Pillow | 13.0.0 | MIT-CMU — https://github.com/python-pillow/Pillow |
| charset-normalizer | 3.5.2 | MIT — https://github.com/jawah/charset_normalizer |
| typing-extensions | 4.16.0 | PSF-2.0 — https://github.com/python/typing_extensions |
| Noto Sans SC (`app/assets/fonts/NotoSansSC.ttf`) | bundled asset | Adobe copyright; SIL Open Font License 1.1; Reserved Font Name `Source`; notice in `app/assets/fonts/NotoSansSC-OFL.txt` |
| Noto Sans KR (`app/assets/fonts/NotoSansKR.ttf`) | bundled asset | Adobe copyright; SIL Open Font License 1.1; Reserved Font Name `Source`; notice in `app/assets/fonts/NotoSansKR-OFL.txt` |
| Noto Color Emoji (`app/assets/fonts/NotoColorEmoji.ttf`) | font Version 2.057; unchanged 2D font asset from `googlefonts/noto-emoji` commit `e20cbc2bbec1926686be9f9bee7d1d2cfa1fea0e` | Copyright 2022 Google Inc.; SIL Open Font License 1.1, original notice in `app/assets/fonts/NotoColorEmoji-OFL.txt` — https://github.com/googlefonts/noto-emoji |
| Flutter SDK | 3.47.5, revision 6a19cca56475dbfba1478ee68d7bd0c2ef891da1 | BSD-3-Clause and bundled notices — https://github.com/flutter/flutter |
| Dart SDK | 3.13.4, included in that Flutter SDK | BSD-3-Clause and bundled notices — https://github.com/dart-lang/sdk |
| http | 1.6.0 | BSD-3-Clause — https://github.com/dart-lang/http |
| file_selector | 1.0.4 | BSD-3-Clause — https://github.com/flutter/packages |
| shared_preferences | 2.5.3 | BSD-3-Clause — https://github.com/flutter/packages |
| web | 1.1.1 | BSD-3-Clause — https://github.com/dart-lang/web |
| record | 7.1.2 | BSD-3-Clause — https://github.com/llfbandit/record |
| path_provider | 2.1.6 | BSD-3-Clause — https://github.com/flutter/packages |
| video_player | 2.14.0 | BSD-3-Clause; Copyright 2013 The Flutter Authors — https://github.com/flutter/packages/tree/main/packages/video_player/video_player |
| video_player_win | 3.3.0 | BSD-3-Clause; Copyright 2022, jakky1 — https://github.com/jakky1/video_player_win |
| crypto | 3.0.7 | BSD-3-Clause; Copyright 2015, the Dart project authors — https://github.com/dart-lang/core/tree/main/pkgs/crypto |
| cupertino_icons | 1.0.9 | MIT; Copyright (c) 2016 Vladimir Kharlampidi — https://github.com/flutter/packages/tree/main/third_party/packages/cupertino_icons |
| Microsoft.Windows.ImplementationLibrary (WIL) | 1.0.220914.1; native build dependency of video_player_win | MIT; Copyright (c) Microsoft Corporation. All rights reserved. Retain its complete LICENSE with distributions — https://github.com/microsoft/wil |

The product API Python dependencies are fixed in `server/requirements.txt`;
the isolated CPU-only Media speech runtime is pinned in
`server/media_speech_requirements.txt` and is not installed into the API
environment. Flutter's resolved dependency graph and package hashes are in
`app/pubspec.lock`. Transitive packages and the Flutter engine retain their
distributed license files; those notices
must accompany distributable builds. This register does not claim that packaging,
third-party notice bundling or full-product release has passed.

PDF generation embeds glyph subsets from the approved Noto Sans SC/KR font assets.
Keep each font's complete OFL 1.1 notice with the font, preserve its Reserved Font
Name requirements for modified font software, and verify packaged font files
match their source notices. ReportLab and spreadsheet/presentation package license
files from the pinned distributions must accompany any packaged runtime.
The et-xmlfile distribution also includes Python standard-library-derived code;
retain its `LICENCE.python` notice with the pinned package license files.

lxml is used directly for safe, structure-preserving Office XML edits. Its actual
6.1.3 distribution includes additional notices for ElementTree/PSF-derived code
and bundled zlib, iconv (LGPL 2.1), libxml2, libxslt and libexslt. A runtime
distribution must retain the corresponding license texts and fulfill the actual
binary distribution obligations; the lxml BSD label alone is not a blanket
license determination for its wheel. Developer test helpers and unneeded
schematron resources are not required by OEVIRA's document operations.

The current `pdfplumber` distribution requires `pypdfium2`; the locked Windows
wheel includes PDFium and an exact `data/windows_x64/BUILD_LICENSES/` set. Preserve
the complete wheel license tree, including PDFium, binary-builder and bundled
library notices, with any distributed binary. pypdfium2's documentation/examples
are separately CC-BY-4.0; its code label does not relicense those materials or
PDFium dependencies. This register does not certify a redistribution package.

The unchanged Noto Color Emoji TTF has SHA-256
`15671215ab769fdc7162a045d56fd7d7e477c51b04e6b3c761d914d8fdd6cc44`;
its original OFL file has SHA-256
`6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2`.
The local Web font fallback uses this bundled asset without a runtime font
download. Preserve the original font and license bytes and expose their
copyright and license through the application's license registry. The font is
not sold by itself or relicensed as OEVIRA-owned artwork.

## Browser presentation transport

The installed Playwright distribution includes its Apache-2.0 license,
`playwright/driver/package/NOTICE` (Microsoft and Puppeteer attribution), the
driver's package license, and `playwright/driver/LICENSE` for Node.js and bundled
components. Retain the complete applicable files with any distributed driver;
the Playwright Apache label does not replace the driver's third-party notices.
Browser-engine distribution rights are separate from the Python package.

The isolated browser remains the installed Chrome/Chromium engine controlled by
Playwright 1.64.0. OEVIRA adds no personal browser profile or public relay service.
The Web viewport uses [aiortc 1.15.0](https://github.com/aiortc/aiortc/tree/1.15.0)
(BSD-3-Clause), with aioice 0.11.0 (BSD-3-Clause), PyAV 17.1.0
(BSD-3-Clause), dnspython 2.9.1 (ISC), google-crc32c 1.9.0 (Apache-2.0),
ifaddr 0.2.0 (MIT), pylibsrtp 1.0.0 (BSD-3-Clause), and pyOpenSSL 26.4.0
(Apache-2.0). Existing cryptography, cffi and pyee dependencies are retained.
No third-party source is modified; versions are pinned in the existing server
requirements and constraints. Retain the distributions' original license texts
and copyright notices when packaging the runtime.

The installed Windows PyAV 17.1.0 wheel dynamically bundles FFmpeg libraries;
its `avcodec_license()` reports **LGPL version 3 or later**. PyAV's BSD license
does not replace those binary obligations. Redistribution must retain the
corresponding FFmpeg/library notices, provide matching source and the LGPL
replacement/relinking rights, and preserve the actual build configuration.

Public-source HTTPS uses [truststore 0.10.5](https://github.com/sethmlarson/truststore)
(MIT) to validate certificates through the operating system's existing trust
store, including its normal intermediate-chain retrieval. No certificate
verification is disabled and no system trust settings are changed.

## Development methods

[The repository pstack skill](.agents/skills/oevira-pstack/SKILL.md) adapts selected
engineering methods from Lauren Tan's pstack 0.15.36, revision
`1ba91fc25c0977741dd0e267849e6234b037ed0a` of `cursor/plugins`.
Its [MIT copyright and license](.agents/skills/oevira-pstack/LICENSE) are retained.
The adaptation keeps task-specific correction, domain modeling, caller-first
architecture, impact verification, direct outcome checks and benchmarking;
it omits upstream model routing, installers, hooks and autonomous effects.
This is development guidance, not an additional product runtime or integration.

ASR weights and the eSpeak binary/voice data are not tracked or included in a
source build; a local ignored `.runtime` may hold the exact Whisper base
checkpoint and eSpeak runtime described above. Product APIs never download
weights or runtime files. A distributable package that includes these local
runtime assets must retain their complete licenses and meet the corresponding
source, relinking and notice requirements. Any newly drawn brand illustrations
are original source assets, not recovered historical artwork.

Before adopting an already planned component, record its exact version/source,
license, modifications and attribution requirements alongside the actual usage.
Preserve required original notices in distributed packages. Product, code, data,
model weights and media rights must not be conflated.

Historical notices are retained verbatim in
[docs/reference/THIRD_PARTY_NOTICES.md](docs/reference/THIRD_PARTY_NOTICES.md).
Their old relative links address the immutable pre-rebuild tree linked from
[the reference index](docs/reference/README.md), not this new source tree.

The root [LICENSE](LICENSE) is unchanged. Self-developed product logic does not
imply that third-party components have been relicensed or privately owned.

NetworkX 3.7 is used by the observed relationship/propagation analysis. It is
Copyright (c) 2004–2026 NetworkX Developers and distributed under BSD-3-Clause.
Preserve its packaged license and notices in distributions. Sources:
[project release](https://pypi.org/project/networkx/3.7/) and
[upstream license](https://github.com/networkx/networkx/blob/networkx-3.7/LICENSE.txt).

## Statistical, financial and time-series analysis

The CPU analysis methods use the exact direct pins in `server/requirements.txt`
and the Windows CPython 3.13 transitive pins in `server/constraints.txt`.
The installed distributions' license files were checked in the selected `.venv`.
Retain the original distribution license files and notices when packaging these
dependencies; the links below identify their upstream projects and releases.

| Package | Pinned version | License and distribution notes | Source |
|---|---:|---|---|
| NumPy | 2.5.4 | BSD-3-Clause, 0BSD, MIT, Zlib and CC0-1.0 components; retain the distribution's complete `licenses/` tree. | [release](https://pypi.org/project/numpy/2.5.4/) |
| SciPy | 1.18.1 | BSD-3-Clause. The installed Windows wheel also declares bundled OpenBLAS (BSD-3-Clause), LAPACK (BSD-3-Clause-Open-MPI), and statically linked GCC runtime (GPL-3.0-or-later WITH GCC-exception-3.1); retain the wheel's full license text and corresponding notices. | [release](https://pypi.org/project/scipy/1.18.1/) |
| PyMC | 6.4.0 | Apache-2.0. | [release](https://pypi.org/project/pymc/6.4.0/) |
| ArviZ | 1.3.0 | Apache-2.0; the split `arviz-base` 1.3.1, `arviz-plots` 1.3.2 and `arviz-stats` 1.3.3 distributions also carry Apache-2.0 notices. | [project](https://github.com/arviz-devs/arviz) |
| PyTensor | 3.3.7 | BSD-3-Clause. | [release](https://pypi.org/project/pytensor/3.3.7/) |
| statsmodels | 0.15.0 | BSD-3-Clause; retain bundled subcomponent notices included in the distribution. | [release](https://pypi.org/project/statsmodels/0.15.0/) |
| ruptures | 1.1.10 | BSD-2-Clause. | [release](https://pypi.org/project/ruptures/1.1.10/) |
| xarray | 2026.9.0 | Apache-2.0. | [release](https://pypi.org/project/xarray/2026.9.0/) |
| xarray-einstats | 0.11.0 | Apache-2.0. | [project](https://github.com/arviz-devs/xarray-einstats) |

The locked statistical runtime also includes these transitive distributions;
their exact versions are in `server/constraints.txt`:

| Packages | License(s) |
|---|---|
| Pygments 2.21.0 | BSD-2-Clause |
| better-optimize 0.5.1; cachetools 6.2.6; filelock 4.2.7; formulaic 1.2.2; interface-meta 2.0.1; mdurl 0.1.2; narwhals 2.28.0; rich 15.0.0; setuptools 84.2.1 | MIT |
| cloudpickle 3.1.2; joblib 1.6.0; lazy-loader 0.6; threadpoolctl 3.7.0; python-dateutil 4.0.0 | BSD-3-Clause |
| llvmlite 0.50.0 | BSD-2-Clause AND Apache-2.0 WITH LLVM-exception; retain `LICENSE.thirdparty` as well. |
| markdown-it-py 4.2.0; wrapt 2.5.1 | MIT; BSD-2-Clause, respectively. |
| numba 0.68.0 | BSD; retain `LICENSES.third-party`. |
| packaging 26.3 | Apache-2.0 OR BSD-2-Clause; both license texts are distributed. |
| pandas 3.1.0 | BSD-3-Clause; retain its bundled third-party license directory. |
| patsy 1.0.3 | BSD-2-Clause |
| tzdata 2026.6 | Apache-2.0 |

SciPy and NumPy notices above describe the exact installed distributions, not a
separate redistribution review. Any shipped runtime must carry the applicable
original license texts and notices for the actual packaged wheels and binaries.
