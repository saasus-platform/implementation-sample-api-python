# dependencies.py

from fastapi import Request, HTTPException
from typing import Union
from saasus_sdk_python.middleware.middleware import Authenticate

# SaaSusの認証インスタンスを作成
auth = Authenticate()


# FastAPI用の認証メソッド
async def fastapi_auth(request: Request) -> Union[dict, HTTPException]:
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.replace("Bearer ", "") if "Bearer " in auth_header else ""
    referer = request.headers.get("X-Saasus-Referer", "")
    user_info, error = auth.authenticate(id_token=token, referer=referer)
    if error:
        raise HTTPException(status_code=401, detail=str(error))
    return user_info


# ユーザーが指定したテナントに所属しているかを確認する
# tenantsはSaaSusの認証ユーザーオブジェクトに含まれるテナントリスト
def belonging_tenant(tenants: dict, tenant_id: str):
    return any(tenant.id == tenant_id for tenant in tenants)
