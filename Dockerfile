FROM python:3.6.5-alpine

# install the result handler and reactor module /usr/local/bin
RUN apk update ; \
    apk upgrade ; \
    apk add git bash build-base ; \
    echo $PATH ; \
    git clone https://github.com/bitsofinfo/testssl.sh-alerts.git ; \
    cp /testssl.sh-alerts/*.py /usr/local/bin/ ; \
    cp -R /testssl.sh-alerts/reactors /usr/local/bin/ ; \
    rm -rf /testssl.sh-alerts ; \
    pip install --upgrade pip objectpath pyyaml python-dateutil watchdog slackclient pygrok jinja2 ; \
    easy_install --upgrade pytz ; \
    cd /tmp ; \
    apk del git build-base ; \
    ls -al /usr/local/bin ; \
    rm -rf /var/cache/apk/* ; \
    chmod +x /usr/local/bin/*.py
