FROM --platform=linux/amd64 ubuntu:22.04 AS base

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
# Install a specific version of Chrome.
RUN wget --progress=dot:giga --timeout=30 --tries=3 https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_157.0.8103.38-1_amd64.deb
RUN echo "a62b277dc03da2cff50cc04589deb66f6875be339c066d46adb49b49eedb629d  google-chrome-stable_157.0.8103.38-1_amd64.deb" | sha256sum -c -
RUN apt-get install -y ./google-chrome-stable_157.0.8103.38-1_amd64.deb

# Install a specific version of ChromeDriver.
RUN wget -q https://storage.googleapis.com/chrome-for-testing-public/157.0.8103.38/linux64/chromedriver-linux64.zip \
    && echo "329525fa0bf02be19794de1e9475772ef64695e0456f142888bd70df3147dc1e  chromedriver-linux64.zip" | sha256sum -c - \
    && unzip chromedriver-linux64.zip \
    && mv chromedriver-linux64/chromedriver /usr/local/bin/chromedriver \
    && chmod +x /usr/local/bin/chromedriver \
    && rm -rf chromedriver-linux64 chromedriver-linux64.zip

RUN python3 -c 'import pathlib, re, sys; p = pathlib.Path("/usr/local/bin/chromedriver"); b = p.read_bytes(); m = re.search(rb"\{window\.cdc_[A-Za-z0-9_]+ = window\.Array;[^}]*\}", b); m or sys.exit("chromedriver automation marker block not found"); p.write_bytes(b[:m.start()] + b"{}".ljust(len(m.group(0))) + b[m.end():])'

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

# Copy only requirements.txt first to leverage Docker cache
COPY requirements.txt .
RUN pip install -r requirements.txt

ENV TINI_VERSION=v0.19.0
ADD https://github.com/krallin/tini/releases/download/${TINI_VERSION}/tini /tini
RUN echo "93dcc18adc78c65a028a84799ecf8ad40c936fdfc5f2a57b1acda5a8117fa82c  /tini" | sha256sum -c -
RUN chmod +x /tini

WORKDIR /opt

FROM deps AS build

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
RUN mkdir -p /etc/opt/chrome/policies/managed \
  && ln -s /tmp/attendee-chrome-policies.json /etc/opt/chrome/policies/managed/attendee-chrome-policies.json

# Switch to non-root AFTER copies to avoid permission flakiness
USER app

# Use tini + entrypoint; CMD can be overridden by compose
ENTRYPOINT ["/tini","--","/usr/local/bin/entrypoint.sh"]
CMD ["bash"]