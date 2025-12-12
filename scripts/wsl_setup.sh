#!/bin/bash
# WSL Setup Script for Attendee
# This script installs all necessary dependencies to run Attendee without Docker on WSL

set -e

echo "==================================="
echo "Attendee WSL Setup Script"
echo "==================================="
echo ""

# Check if running on WSL
if ! grep -qi microsoft /proc/version 2>/dev/null; then
    echo "Warning: This script is designed for WSL. Continuing anyway..."
fi

# Update system
echo "[1/10] Updating system packages..."
sudo apt-get update

# Install build essentials and core dependencies
echo "[2/10] Installing build essentials and core dependencies..."
sudo apt-get install -y \
    build-essential \
    ca-certificates \
    cmake \
    curl \
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
    python3-venv \
    tar \
    unzip \
    zip \
    vim \
    libpq-dev

# Install Chrome dependencies
echo "[3/10] Installing Chrome dependencies..."
sudo apt-get install -y \
    xvfb \
    x11-xkb-utils \
    xfonts-100dpi \
    xfonts-75dpi \
    xfonts-scalable \
    xfonts-cyrillic \
    x11-apps \
    libvulkan1 \
    fonts-liberation \
    xdg-utils \
    wget

# Install Chrome browser
echo "[4/10] Installing Google Chrome..."
if ! command -v google-chrome &> /dev/null; then
    wget -q http://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_134.0.6998.88-1_amd64.deb -O /tmp/chrome.deb
    sudo apt-get install -y /tmp/chrome.deb
    rm /tmp/chrome.deb
else
    echo "Chrome already installed, skipping..."
fi

# Install ChromeDriver
echo "[5/10] Installing ChromeDriver..."
if ! command -v chromedriver &> /dev/null; then
    wget -q https://storage.googleapis.com/chrome-for-testing-public/134.0.6998.88/linux64/chromedriver-linux64.zip -O /tmp/chromedriver.zip
    unzip -q /tmp/chromedriver.zip -d /tmp/
    sudo mv /tmp/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver
    sudo chmod +x /usr/local/bin/chromedriver
    rm -rf /tmp/chromedriver-linux64 /tmp/chromedriver.zip
else
    echo "ChromeDriver already installed, skipping..."
fi

# Install audio dependencies
echo "[6/10] Installing audio dependencies (ALSA, PulseAudio, FFmpeg)..."
sudo apt-get install -y \
    libasound2 \
    libasound2-plugins \
    alsa-utils \
    pulseaudio \
    pulseaudio-utils \
    ffmpeg

# Install GStreamer
echo "[7/10] Installing GStreamer..."
sudo apt-get install -y \
    gstreamer1.0-alsa \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    libgirepository1.0-dev \
    --fix-missing

# Install additional tools
echo "[8/10] Installing additional tools..."
sudo apt-get install -y \
    universal-ctags \
    xterm \
    xmlsec1 \
    xclip \
    libavdevice-dev

# Install PostgreSQL
echo "[9/10] Installing PostgreSQL..."
sudo apt-get install -y postgresql postgresql-contrib
sudo systemctl enable postgresql || true

# Install Redis
echo "[10/10] Installing Redis..."
sudo apt-get install -y redis-server
sudo systemctl enable redis-server || true

# Update CA certificates
sudo update-ca-certificates

echo ""
echo "==================================="
echo "System dependencies installed!"
echo "==================================="
echo ""
echo "Next steps:"
echo ""
echo "1. Set up PostgreSQL database:"
echo "   sudo -u postgres psql -c \"CREATE USER attendee_development_user WITH PASSWORD 'attendee_development_user';\""
echo "   sudo -u postgres psql -c \"CREATE DATABASE attendee_development OWNER attendee_development_user;\""
echo "   sudo -u postgres psql -c \"ALTER USER attendee_development_user CREATEDB;\""
echo ""
echo "2. Start services:"
echo "   sudo service postgresql start"
echo "   sudo service redis-server start"
echo ""
echo "3. Set up Python environment:"
echo "   cd $(dirname "$0")/.."
echo "   python3 -m venv venv"
echo "   source venv/bin/activate"
echo "   pip install -r requirements.txt"
echo "   pip uninstall -y av && pip install --no-binary av 'av==12.0.0'"
echo ""
echo "4. Generate environment file:"
echo "   python init_env.py > .env"
echo "   # Edit .env and add your AWS credentials"
echo ""
echo "5. Run migrations:"
echo "   python manage.py migrate"
echo ""
echo "6. Start the application:"
echo "   ./scripts/run_local.sh"
echo ""
