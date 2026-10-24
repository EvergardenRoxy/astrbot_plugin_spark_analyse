# Third-party attribution

`spark_core/proto/spark.proto` and `spark_sampler.proto` originate from lucko/spark, revision `03210f75c3b040f1bf7b369761573b83c75bb5ce`, https://github.com/lucko/spark . Copyright lucko (Luck) and contributors. License: GPL-3.0-or-later; complete upstream license is included as LICENSE.

The sampler schema import was changed from `spark/spark.proto` to `spark.proto` solely for local generation. Generated `*_pb2.py` files produced with grpcio-tools 1.78.0 (protoc Python 6.31.1); generated Python import made package-relative. Development reproduction: `python tools/build_schema.py`; not run at plugin startup.

Plugin and core source are distributed under GPL-3.0-or-later. aiohttp and protobuf are runtime dependencies, not vendored implementations; their respective upstream licenses apply. No code from spark-analyzer or spark-profiler-mcp is bundled. No reference report or credentials are included.

`spark_core/chart_assets/SparkChartSans.otf` is a modified subset of Noto Sans CJK SC Regular, originally
Copyright 2010-2012 Google Corporation, distributed under SIL Open Font License 1.1. Source:
https://github.com/notofonts/noto-cjk (Debian `fonts-noto-cjk` installed font collection, SC face).
The modified family/PostScript name is Spark Chart Sans / SparkChartSans-Regular. Latin, CJK ideographs,
punctuation and full-width symbols were subset with fontTools; original glyph outlines were not edited.
Full license and attribution are in `spark_core/chart_assets/OFL.txt`. This font remains under OFL, not GPL.

The chart Sources accounting follows the outermost-per-source inclusive algorithm and mapping priorities
documented by lucko/spark-viewer, revision `05d68bda52ef483122c1498794dbc2cb5e3ada78`:
https://github.com/lucko/spark-viewer/blob/05d68bda52ef483122c1498794dbc2cb5e3ada78/src/viewer/sampler/worker/SourceViewGenerator.ts
and `src/viewer/sampler/data/SourcesMap.ts`. Upstream: copyright lucko and contributors, GPLv3.
The Python traversal is implemented here; no viewer assets or sample reports are bundled.

Matplotlib and tzdata are additional runtime dependencies and are not vendored. Their licenses apply separately.
