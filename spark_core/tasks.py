import asyncio

async def cancel_bounded(tasks, timeout=5):
    pending = {t for t in tasks if not t.done() and t is not asyncio.current_task()}
    for task in pending:
        task.cancel()
    if not pending:
        return set()
    _, remaining = await asyncio.wait(pending, timeout=timeout)
    for task in remaining:
        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
    return remaining
