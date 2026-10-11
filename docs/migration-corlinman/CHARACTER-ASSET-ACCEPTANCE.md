# Knights College Character Asset Acceptance

Read-only local validation completed 2026-08-20 against an external delivery
directory containing the eleven source assets listed below. No asset was copied
into this repository and no source was downloaded during this check.

Deploy the eleven accepted files under `$HERMES_HOME/characters`. In
production, `HERMES_HOME=/opt/hermes/data`, so their resolved directory is
`/opt/hermes/data/characters`.

Validation commands decoded each file rather than trusting its suffix:

```text
file <asset>
sips -g pixelWidth -g pixelHeight -g format -g hasAlpha <asset>
identify -format '%f | %m | %wx%h | channels=%[channels] | opaque=%[opaque]' <asset>
```

`file` reported `PNG image data` for every target. `identify` reported `PNG`,
`srgba`, and `opaque=False` for every target, so each file is a real RGBA PNG
with transparent pixels. Source classification and URLs are the delivery record
in the adjacent local `MANIFEST.md`; this check did not re-fetch or independently
re-license those sources.

| Target / CHARACTER_KEYS key | Exists | Dimensions | Mode / alpha | Local delivery source type | Verdict |
| --- | --- | --- | --- | --- | --- |
| `01_algo_northrop.png` / `algo` | yes | 901x901 | sRGBA, transparent | game original sprite | pass |
| `02_grantley_bell.png` / `grantley` | yes | 1041x1080 | sRGBA, transparent | game original sprite | pass |
| `03_oscar_lawrence.png` / `oscar` | yes | 846x846 | sRGBA, transparent | game original sprite | pass |
| `04_diedrich_olsen.png` / `diedrich` | yes | 1100x688 | sRGBA, transparent | official character card | pass by documented 688px exception; card text retained, not cropped |
| `05_paul_pfizner.png` / `paul` | yes | 944x944 | sRGBA, transparent | game original sprite | pass |
| `06_theo_prince.png` / `theo` | yes | 894x894 | sRGBA, transparent | game original sprite | pass |
| `07_julius_kinial.png` / `julius` | yes | 1013x953 | sRGBA, transparent | game original sprite | pass |
| `08_hermann_furst.png` / `hermann` | yes | 942x942 | sRGBA, transparent | game original sprite | pass |
| `09_helio_delatre.png` / `helio` | yes | 1100x688 | sRGBA, transparent | official character card | pass by documented 688px exception; card text retained, not cropped |
| `10_shayat.png` / `shayat` | yes | 1080x1080 | sRGBA, transparent | game original sprite | pass |
| `11_bating.png` / `bating` | yes | 1142x1080 | sRGBA, transparent | game original sprite | pass |

The two 688px files do **not** meet the normal `short edge >= 800px` rule. They
are accepted only under the documented official-character-card exception, with
their card text and full 1100x688 composition preserved.

The known invalid Q-version files remain outside the usable set:
`INVALID-q版推测立绘.png` and `demo-INVALID-q版推测/demo1.png` through
`demo4.png` are not in `knights-college/`, do not match a fixed
`CHARACTER_KEYS` filename, and are never returned by `available_characters()`.
The runtime registry additionally decodes candidate mapped files before it
advertises them, so a corrupt replacement cannot enter the usable list.

## KC2 replacement (2026-10-10)

The set above is superseded by fourteen Knights College 2 sprites, built by
[`build_assets.py`](../personas/grantley-kc2-20261010/build_assets.py) from the
user's local game export. Each target is the character's KC2 `10000` pattern
(uniform, neutral face) from `完整立绘_全部差分`, copied byte for byte. That makes
every file checkable against the export's own `图片/校验清单.sha256`, and all
fourteen match it. Nothing was redrawn, upscaled, cropped or generated.
Provenance per file (source path, CRC32, SHA256) is in
[`assets-manifest.json`](../personas/grantley-kc2-20261010/assets-manifest.json).

Filenames for the first eleven keys are unchanged, so the deployed paths stay
the same. `felix`, `gamir` and `bayfleet` are KC2's new main cast. Pillow
decoded every file as a PNG in RGBA mode with transparent pixels.

| Target / CHARACTER_KEYS key | Dimensions | Mode / alpha | KC2 source | SHA256 (prefix) | Verdict |
| --- | --- | --- | --- | --- | --- |
| `01_algo_northrop.png` / `algo` | 1224x1080 | RGBA, transparent | 亚戈 `10000` | `e2de6a0dea69` | pass |
| `02_grantley_bell.png` / `grantley` | 1168x1125 | RGBA, transparent | 格兰特利 `10000` | `fa32a7893818` | pass |
| `03_oscar_lawrence.png` / `oscar` | 1168x1032 | RGBA, transparent | 奥斯卡 `10000` | `d78251cd7197` | pass |
| `04_diedrich_olsen.png` / `diedrich` | 1472x1080 | RGBA, transparent | 迪德里奇 `10000` | `44c72ff9e9e8` | pass |
| `05_paul_pfizner.png` / `paul` | 1168x1160 | RGBA, transparent | 保罗 `10000` | `97d8f63e889f` | pass |
| `06_theo_prince.png` / `theo` | 1256x1080 | RGBA, transparent | 西奥 `10000` | `671c840c2b71` | pass |
| `07_julius_kinial.png` / `julius` | 1168x1122 | RGBA, transparent | 尤利乌斯 `10000` | `b17c5cda8205` | pass |
| `08_hermann_furst.png` / `hermann` | 1100x1117 | RGBA, transparent | 赫尔曼 `10000` | `7d1f51be4e7e` | pass |
| `09_helio_delatre.png` / `helio` | 1256x1120 | RGBA, transparent | 希利欧 `10000` | `f066912395f1` | pass |
| `10_shayat.png` / `shayat` | 1216x1155 | RGBA, transparent | 沙亚特 `10000` | `a5bbcd9dd753` | pass |
| `11_bating.png` / `bating` | 1621x1110 | RGBA, transparent | 巴亭 `10000` | `7843848c014c` | pass |
| `12_felix.png` / `felix` | 1556x1100 | RGBA, transparent | 菲力克斯 `10000` | `c30b4c5031b3` | pass (new key) |
| `13_gamir.png` / `gamir` | 1140x1080 | RGBA, transparent | 伽米尔 `10000` | `b3f04e45b967` | pass (new key) |
| `14_bayfleet.png` / `bayfleet` | 1556x1080 | RGBA, transparent | 贝弗利特 `10000` | `2037ef5f83f8` | pass (new key) |

Every short edge is at least 1032 px, so the 688px official-card exception
above no longer applies to any deployed file. Deployed to P1 on 2026-10-11 as part of the
[persona revision](../personas/grantley-kc2-20261010/README.md#部署记录); the previous
directory is in `/opt/hermes/backups/persona-kc2-20261011T090803Z/characters.tar`.
