import logging
import httpx

logger = logging.getLogger("smm")

SMM_BASE = "https://twiboost.com/api/v2"


class SMMError(Exception):
    pass


async def create_order(smm_key: str, service_id: int, link: str, quantity: int) -> int:
    """
    Создаёт заказ в SMMWay.
    Возвращает smm_order_id.
    """
    params = {
        "action": "add",
        "key": smm_key,
        "service": service_id,
        "link": link,
        "quantity": quantity,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(SMM_BASE, params=params)
        resp.raise_for_status()
        data = resp.json()

    if "error" in data:
        raise SMMError(f"SMMWay error: {data['error']}")
    if "order" not in data:
        raise SMMError(f"SMMWay unexpected response: {data}")

    order_id = int(data["order"])
    logger.info(f"SMM order created: {order_id}")
    return order_id


async def get_status(smm_key: str, smm_order_id: int) -> dict:
    """
    Возвращает dict с полями: status, charge, start_count, remains, currency
    Возможные статусы от smmway: Pending | In progress | Completed | Partial | Canceled | Fail
    """
    params = {
        "action": "status",
        "key": smm_key,
        "order": smm_order_id,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(SMM_BASE, params=params)
        resp.raise_for_status()
        data = resp.json()

    if "error" in data:
        raise SMMError(f"SMMWay status error: {data['error']}")

    return data
