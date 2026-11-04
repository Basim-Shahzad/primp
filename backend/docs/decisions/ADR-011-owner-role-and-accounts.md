# ADR-011: Business owner accounts

- **One account, one `role` field.** An existing customer who calls `POST /users/me/become-owner` (verified phone required) gets `role = business_owner`. The role is not removed again by this flow.
- **Customer abilities are retained.** Customer endpoints (booking, holds, reviews, wishlist, profile) accept `CUSTOMER_CAPABLE = (customer, business_owner)` instead of only `customer`. Admins and staff remain excluded from customer endpoints. Provider endpoints accept only `business_owner`.
- Plan 6.1 tests that used a business owner as the "wrong role" for customer endpoints now use an admin; owner-positive cases are covered in `test_owner.py`.
- `POST /auth/register/business-owner` proves the phone with the existing login OTP (`otp.LOGIN`, requested through `/auth/otp/request`); no second OTP implementation. It is limited per IP by the same registration limiter as customer registration.
- Not decided here (out of scope): preventing an owner from booking their own business.
