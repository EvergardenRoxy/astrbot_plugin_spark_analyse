import logging
import unittest
from spark_core.logging_scope import SparkToolLogFilter, spark_analysis_logging

class LoggingTests(unittest.TestCase):
    def test_scoped_downgrade(self):
        logger = logging.getLogger('spark-test')
        previous = logger.level
        logger.setLevel(logging.INFO)
        f = SparkToolLogFilter()
        def record(msg, level=logging.INFO):
            return logging.LogRecord('spark-test', level, '/sdk/tool_loop_agent_runner.py', 1380, msg, (), None)
        try:
            token = spark_analysis_logging.set(True)
            try:
                r = record('Tool `spark_query` Result: huge payload')
                self.assertFalse(f.filter(r))
                self.assertEqual(r.levelno, logging.DEBUG)
                logger.setLevel(logging.DEBUG)
                self.assertTrue(f.filter(record('使用工具：spark_query，参数：{}')))
                self.assertTrue(f.filter(record('provider failed', logging.WARNING)))
                self.assertEqual(record('other event').levelno, logging.INFO)
            finally:
                spark_analysis_logging.reset(token)
            r = record('Tool `other` Result: untouched')
            self.assertTrue(f.filter(r))
            self.assertEqual(r.levelno, logging.INFO)
        finally:
            logger.setLevel(previous)
