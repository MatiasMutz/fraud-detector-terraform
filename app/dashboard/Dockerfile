FROM alpine:3.21 AS build

ARG API_BASE=http://localhost:5000
ARG COGNITO_CLIENT_ID=
ARG COGNITO_DOMAIN=
ARG COGNITO_REDIRECT_URI=
ARG COGNITO_LOGOUT_URI=
ARG COGNITO_USER_POOL_ID=
ARG COGNITO_ISSUER=

WORKDIR /site

COPY dashboard/index.html dashboard/app.js dashboard/favicon.ico ./

RUN printf '%s\n' \
    '// Dashboard runtime config - generated during the dashboard build.' \
    "window.API_BASE = \"${API_BASE}\";" \
    'window.COGNITO_CONFIG = {' \
    "  clientId: \"${COGNITO_CLIENT_ID}\"," \
    "  domain: \"${COGNITO_DOMAIN}\"," \
    "  redirectUri: \"${COGNITO_REDIRECT_URI}\"," \
    "  logoutUri: \"${COGNITO_LOGOUT_URI}\"," \
    "  userPoolId: \"${COGNITO_USER_POOL_ID}\"," \
    "  issuer: \"${COGNITO_ISSUER}\"" \
    '};' \
    'window.COGNITO = window.COGNITO_CONFIG;' \
    'window.AUTH_CONFIG = window.COGNITO_CONFIG;' \
    > config.js

FROM scratch AS export

COPY --from=build /site/ /

FROM nginx:1.27-alpine AS runtime

COPY --from=export /index.html /usr/share/nginx/html/index.html
COPY --from=export /app.js /usr/share/nginx/html/app.js
COPY --from=export /config.js /usr/share/nginx/html/config.js
COPY --from=export /favicon.ico /usr/share/nginx/html/favicon.ico

EXPOSE 80
