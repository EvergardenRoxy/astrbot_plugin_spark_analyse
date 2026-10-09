# Third-party attribution

`spark_core/proto/spark.proto` and `spark_sampler.proto` originate from lucko/spark, revision `03210f75c3b040f1bf7b369761573b83c75bb5ce`, https://github.com/lucko/spark . Copyright lucko (Luck) and contributors. License: GPL-3.0-or-later; complete upstream license is included as LICENSE.

The sampler schema import was changed from `spark/spark.proto` to `spark.proto` solely for local generation. Generated `*_pb2.py` files produced with grpcio-tools 1.78.0 (protoc Python 6.31.1); generated Python import made package-relative. Development reproduction: `python tools/build_schema.py`; not run at plugin startup.

Plugin and core source are distributed under GPL-3.0-or-later. aiohttp and protobuf are runtime dependencies, not vendored implementations; their respective upstream licenses apply. No code from spark-analyzer or spark-profiler-mcp is bundled. No reference report or credentials are included.
