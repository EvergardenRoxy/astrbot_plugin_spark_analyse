"""Downgrade only Spark-task runner tool chatter, never other agents or errors."""
import logging
from contextvars import ContextVar

spark_analysis_logging = ContextVar('spark_analysis_logging', default=False)


class SparkToolLogFilter(logging.Filter):
    def filter(self, record):
        if (spark_analysis_logging.get() and record.levelno == logging.INFO
                and record.pathname.replace('\\', '/').endswith('/tool_loop_agent_runner.py')
                and record.getMessage().startswith(('Agent 使用工具:', '使用工具：', 'Tool `'))):
            record.levelno = logging.DEBUG
            record.levelname = 'DEBUG'
            record.short_levelname = 'DBUG'
            return logging.getLogger(record.name).isEnabledFor(logging.DEBUG)
        return True
