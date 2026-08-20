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
