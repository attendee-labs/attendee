FROM ubuntu:22.04 AS base

ARG TARGETARCH

SHELL ["/bin/bash", "-c"]

ENV project=attendee
ENV cwd=/$project

WORKDIR $cwd

ARG DEBIAN_FRONTEND=noninteractive

#  Install Dependencies
RUN apt-get update  \
    && apt-get install -y \
    build-essential \
    ca-certificates \
    cmake \
    curl \
    gdb \
    git \
    gfortran \
    libopencv-dev \
    libdbus-1-3 \
    libgbm1 \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libglib2.0-dev \
    libssl-dev \
    libx11-dev \
    libx11-xcb1 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-randr0 \
    libxcb-shape0 \
    libxcb-shm0 \
    libxcb-xfixes0 \
    libxcb-xtest0 \
    libgl1-mesa-dri \
    libxfixes3 \
    linux-libc-dev \
    pkgconf \
    python3-pip \
    tar \
    unzip \
    zip \
    vim \
    libpq-dev

# Install Chrome dependencies
RUN apt-get install -y xvfb xauth x11-xkb-utils xfonts-100dpi xfonts-75dpi xfonts-scalable xfonts-cyrillic x11-apps libvulkan1 fonts-liberation xdg-utils wget
# Install a specific version of Chrome and ChromeDriver.
# Google does not publish Chrome or ChromeDriver for linux/arm64, so on arm64 we install Debian bookworm's
# Chromium build of the same version (134.0.6998.88) and symlink it into the paths where Chrome lives on amd64.
RUN if [ "$TARGETARCH" = "amd64" ]; then \
        wget --progress=dot:giga --timeout=30 --tries=3 https://build-assets.attendee.dev/google-chrome/pool/main/g/google-chrome-stable/google-chrome-stable_134.0.6998.88-1_amd64.deb \
        # Verify that the package is correct, since this is a mirror.
        && echo "df557edb3d24d8dcaff9557d80733b42afb6626685200d3f34a3b6f528065cad  google-chrome-stable_134.0.6998.88-1_amd64.deb" | sha256sum -c - \
        && apt-get install -y ./google-chrome-stable_134.0.6998.88-1_amd64.deb \
        && wget -q https://storage.googleapis.com/chrome-for-testing-public/134.0.6998.88/linux64/chromedriver-linux64.zip \
        && echo "58df717d51484b9f3ac188af5231cdc77255daa72d0b2b86481bee54e398ce2f  chromedriver-linux64.zip" | sha256sum -c - \
        && unzip chromedriver-linux64.zip \
        && mv chromedriver-linux64/chromedriver /usr/local/bin/chromedriver \
        && chmod +x /usr/local/bin/chromedriver \
        && rm -rf chromedriver-linux64 chromedriver-linux64.zip; \
    elif [ "$TARGETARCH" = "arm64" ]; then \
        # Dependencies of the Debian Chromium packages that Ubuntu 22.04 can satisfy.
        apt-get install -y \
            libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libcairo2 libcups2 libdbus-1-3 \
            libdouble-conversion3 libexpat1 libfontconfig1 libfreetype6 libgbm1 libglib2.0-0 libgraphite2-3 \
            libgtk-3-0 liblcms2-2 libminizip1 libnspr4 libnss3 libogg0 libopus0 libpango-1.0-0 libpng16-16 \
            libpulse0 libudev1 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 \
            libxml2 libxnvctrl0 libxrandr2 libxslt1.1 zlib1g x11-utils xdg-utils \
        && CHROMIUM_TMP="$(mktemp -d)" \
        && cd "$CHROMIUM_TMP" \
        && CHROMIUM_POOL=https://snapshot.debian.org/archive/debian-security/20250314T231520Z/pool/updates/main/c/chromium \
        && BOOKWORM_POOL=https://snapshot.debian.org/archive/debian/20250315T150632Z/pool/main \
        && wget --progress=dot:giga --timeout=30 --tries=3 \
            "$CHROMIUM_POOL/chromium_134.0.6998.88-1~deb12u1_arm64.deb" \
            "$CHROMIUM_POOL/chromium-common_134.0.6998.88-1~deb12u1_arm64.deb" \
            "$CHROMIUM_POOL/chromium-driver_134.0.6998.88-1~deb12u1_arm64.deb" \
            "$BOOKWORM_POOL/d/dav1d/libdav1d6_1.0.0-2+deb12u1_arm64.deb" \
            "$BOOKWORM_POOL/f/flac/libflac12_1.4.2+ds-2_arm64.deb" \
            "$BOOKWORM_POOL/libj/libjpeg-turbo/libjpeg62-turbo_2.1.5-2_arm64.deb" \
            "$BOOKWORM_POOL/o/openh264/libopenh264-7_2.3.1+dfsg-3+deb12u2_arm64.deb" \
            "$BOOKWORM_POOL/h/harfbuzz/libharfbuzz0b_6.0.0+dfsg-3_arm64.deb" \
            "$BOOKWORM_POOL/h/harfbuzz/libharfbuzz-subset0_6.0.0+dfsg-3_arm64.deb" \
            "$BOOKWORM_POOL/o/openjpeg2/libopenjp2-7_2.5.0-2+deb12u1_arm64.deb" \
            "$BOOKWORM_POOL/libz/libzstd/libzstd1_1.5.4+dfsg2-5_arm64.deb" \
        && printf '%s\n' \
            "92960c257544cf778fc32d210ce30eda0df505d810145959e1fff8a09e73edb8  chromium_134.0.6998.88-1~deb12u1_arm64.deb" \
            "c378225c7da41cd78cefff8221a753dbb997e818aab9c21a95ffa45c75d171db  chromium-common_134.0.6998.88-1~deb12u1_arm64.deb" \
            "fdc30b76895dadf05b588ed5b0e61e29bf138ca3fd53b80a87fc24b6f39c570c  chromium-driver_134.0.6998.88-1~deb12u1_arm64.deb" \
            "579c820b80ce3491143c411a342b3a222ce3a381c7c052e8c8501de5bace954f  libdav1d6_1.0.0-2+deb12u1_arm64.deb" \
            "338f78e2a140ed4ffca36d4240a7fc6f7867999bea000762a0c1b3924245a868  libflac12_1.4.2+ds-2_arm64.deb" \
            "de66f186f3ff3c1d10c2e75ae056b019b3f7f091f51096a06cade48b2dea875b  libjpeg62-turbo_2.1.5-2_arm64.deb" \
            "82cf558f398825b0ecb409385af8a77a90beb601249097d0cacb17640adf444d  libopenh264-7_2.3.1+dfsg-3+deb12u2_arm64.deb" \
            "64b1d4aa672dc4eda5e11b9ff8061122060fc7aba6ad16908c89a269ffa174ee  libharfbuzz0b_6.0.0+dfsg-3_arm64.deb" \
            "cde543fedd1c0c63b000532ffc80935c9cece2bea505993709d396a9215024d9  libharfbuzz-subset0_6.0.0+dfsg-3_arm64.deb" \
            "fff01770c6372bd962c96a2d80b9fa61cab1b69db7064624d0f83f9e9f8ef77f  libopenjp2-7_2.5.0-2+deb12u1_arm64.deb" \
            "95e173c9538f96ede4fc275ec7863f395a97dd0ea62454be9bc914efa1b9be93  libzstd1_1.5.4+dfsg2-5_arm64.deb" \
            | sha256sum -c - \
        # The Chromium packages are extracted rather than installed with dpkg, because dpkg would record unmet
        # bookworm-only dependencies and break every later apt-get install.
        && for deb in chromium_*.deb chromium-common_*.deb chromium-driver_*.deb; do dpkg-deb -x "$deb" /; done \
        # Libraries that are missing or too old on Ubuntu 22.04 go in a private directory that only Chromium and
        # ChromeDriver load from, so the system copies used by everything else are left untouched.
        && mkdir -p bundled /opt/chromium-bundled-libs \
        && for deb in lib*.deb; do dpkg-deb -x "$deb" bundled; done \
        && cp -a bundled/usr/lib/aarch64-linux-gnu/. /opt/chromium-bundled-libs/ \
        && echo 'export LD_LIBRARY_PATH="/opt/chromium-bundled-libs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"' > /etc/chromium.d/attendee-bundled-libs \
        && printf '%s\n' \
            '#!/bin/sh' \
            'export LD_LIBRARY_PATH="/opt/chromium-bundled-libs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"' \
            'exec /usr/bin/chromedriver "$@"' \
            > /usr/local/bin/chromedriver \
        && chmod +x /usr/local/bin/chromedriver \
        && mkdir -p /opt/google/chrome \
        && ln -s /usr/bin/chromium /opt/google/chrome/chrome \
        && ln -s /usr/bin/chromium /usr/bin/google-chrome \
        && ln -s /usr/bin/chromium /usr/bin/google-chrome-stable \
        && cd / \
        && rm -rf "$CHROMIUM_TMP"; \
    else \
        echo "Unsupported TARGETARCH: $TARGETARCH" && exit 1; \
    fi

# Install ALSA
RUN apt-get update && apt-get install -y libasound2 libasound2-plugins alsa alsa-utils alsa-oss

# Install Pulseaudio
RUN apt-get install -y  pulseaudio pulseaudio-utils ffmpeg

# Install Linux Kernel Dev
RUN apt-get update && apt-get install -y linux-libc-dev

# Update certificates
RUN apt-get update && apt-get install -y \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && update-ca-certificates

# Install Ctags
RUN apt-get update && apt-get install -y universal-ctags

# Install xterm
RUN apt-get update && apt-get install -y xterm

# Install xclip
RUN apt-get update && apt-get install -y xclip

# Install libavdevice-dev. Needed so that webpage streaming using pyav will work.
RUN apt-get update && apt-get install -y libavdevice-dev && pip uninstall -y av && pip install --no-binary av "av==12.0.0"

# Install gstreamer
RUN apt-get install -y gstreamer1.0-alsa gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev libgirepository1.0-dev --fix-missing

# Alias python3 to python
RUN ln -s /usr/bin/python3 /usr/bin/python

FROM base AS deps

ARG TARGETARCH

# Copy only requirements.txt first to leverage Docker cache
COPY requirements.txt .
RUN pip install -r requirements.txt

ENV TINI_VERSION=v0.19.0
ADD https://github.com/krallin/tini/releases/download/${TINI_VERSION}/tini-${TARGETARCH} /tini
RUN if [ "$TARGETARCH" = "amd64" ]; then \
        echo "93dcc18adc78c65a028a84799ecf8ad40c936fdfc5f2a57b1acda5a8117fa82c  /tini" | sha256sum -c -; \
    elif [ "$TARGETARCH" = "arm64" ]; then \
        echo "07952557df20bfd2a95f9bef198b445e006171969499a1d361bd9e6f8e5e0e81  /tini" | sha256sum -c -; \
    else \
        echo "Unsupported TARGETARCH: $TARGETARCH" && exit 1; \
    fi
RUN chmod +x /tini

WORKDIR /opt

FROM deps AS build

ARG TARGETARCH

# Create non-root user
RUN useradd -m -u 1000 -s /bin/bash app

# Workdir owned by app in one shot during copy
ENV project=attendee
ENV cwd=/$project
WORKDIR $cwd

# Copy only what you need; set ownership/perm at copy time
COPY --chown=app:app --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --chown=app:app . .

# Make STATIC_ROOT writeable for the non-root user so collectstatic can run at startup
RUN mkdir -p "$cwd/staticfiles" && chown -R app:app "$cwd/staticfiles"

# We want the app to be able to dynamically set the chrome policies file.
# However, chrome will load the file from a hardcoded path in a directory that the app cannot write to.
# Therefore, we create a symlink at that path that points to a file in /tmp which the app can write to.
# Chromium (used on arm64) reads policies from /etc/chromium instead of /etc/opt/chrome.
RUN mkdir -p /etc/opt/chrome/policies/managed \
  && ln -s /tmp/attendee-chrome-policies.json /etc/opt/chrome/policies/managed/attendee-chrome-policies.json \
  && if [ "$TARGETARCH" = "arm64" ]; then \
       mkdir -p /etc/chromium/policies/managed \
       && ln -s /tmp/attendee-chrome-policies.json /etc/chromium/policies/managed/attendee-chrome-policies.json; \
     fi

# Switch to non-root AFTER copies to avoid permission flakiness
USER app

# Use tini + entrypoint; CMD can be overridden by compose
ENTRYPOINT ["/tini","--","/usr/local/bin/entrypoint.sh"]
CMD ["bash"]