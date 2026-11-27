# Private loans dashboard

Host this FastAPI service on its own HTTPS origin. GitHub Pages serves the
public `loans/` landing page; it does not host the sign-in UI, members list,
or balance API. The dashboard currently returns an empty list after sign-in.

1. Create a Google OAuth **Web application** client. Add the service's
   origin to Authorized JavaScript origins (`http://localhost:8000` for dev).
2. Set `GOOGLE_CLIENT_ID` and `LOANS_MEMBERS` in the service environment.
   The latter is a comma-separated pair of Gmail or Google Workspace addresses.
   Keep actual configuration out of this repository.
3. From this folder in a virtual environment, `pip install -r requirements.txt`,
   then `uvicorn main:app`. Run the access tests with `python -m unittest`.
4. Before linking the service from Pages, verify both members can sign in
   over HTTPS and an unrelated Google account is denied.

## Lender integration contract

Sign-in does not connect lenders. Account discovery must be private to the
person linking the lender. Show individual loans unchecked; publish only the
IDs the owner explicitly confirms via **Share with both of us**. Verify
ownership on the server at confirmation. Return only shared lender names,
balances, currencies and refresh timestamps to the household. Keep lender
tokens server-side and support unsharing. No lender or storage integration
is implemented yet.
