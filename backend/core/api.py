from ninja_extra import NinjaExtraAPI
from ninja_jwt.controller import NinjaJWTDefaultController
from ninja_jwt.authentication import JWTAuth

api = NinjaExtraAPI(title="Primp API", version="1.0.0")

# This registers /api/token/pair and /api/token/refresh endpoints automatically
api.register_controllers(NinjaJWTDefaultController)


@api.get("/health")
def health_check(request):
    return {"status": "healthy", "service": "primp-backend"}
