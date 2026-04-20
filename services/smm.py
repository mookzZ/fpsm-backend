import logging
import httpx

logger = logging.getLogger("smm")

SMM_BASE = "https://bestsmmlike.ru/api/v2"


class SMMError(Exception):
    pass


async def create_order(smm_key: str, service_id: int, link: str, quantity: int) -> int:
    """
    Создаёт заказ в SMM-панели.
    Возвращает smm_order_id.
    """
    data = {
        "action": "add",
        "key": smm_key,
        "service": service_id,
        "link": link,
        "quantity": quantity,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(SMM_BASE, data=data)
        resp.raise_for_status()
        result = resp.json()

    if "error" in result:
        raise SMMError(f"SMM error: {result['error']}")
    if "order" not in result:
        raise SMMError(f"SMM unexpected response: {result}")

    order_id = int(result["order"])
    logger.info(f"SMM order created: {order_id}")
    return order_id


async def get_status(smm_key: str, smm_order_id: int) -> dict:
    """
    Возвращает dict с полями: status, charge, start_count, remains, currency
    Возможные статусы: Pending | In progress | Completed | Partial | Canceled | Fail
    """
    data = {
        "action": "status",
        "key": smm_key,
        "order": smm_order_id,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(SMM_BASE, data=data)
        resp.raise_for_status()
        result = resp.json()

    if "error" in result:
        raise SMMError(f"SMM status error: {result['error']}")

    return result
