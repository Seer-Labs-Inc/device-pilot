# Device Pilot - Motion Detection and Video Capture System
# Makefile for common development tasks

.PHONY: setup setup-mac setup-pi install test test-verbose clean run help

help:
	@echo "Available targets:"
	@echo "  setup-mac    Install Mac dependencies (Homebrew + Python)"
	@echo "  setup-pi     Install Raspberry Pi dependencies (apt + Python)"
	@echo "  install      Install Python package in development mode"
	@echo "  test         Run all tests"
	@echo "  test-verbose Run tests with verbose output"
	@echo "  clean        Remove build artifacts"
	@echo "  run          Run the device-pilot system (requires RTSP URLs)"

# Mac setup
setup-mac:
	@echo "Installing Mac system dependencies..."
	brew install ffmpeg fswatch
	@echo "Installing Python dependencies..."
	pip install -e ".[dev]"
	@echo "Setup complete!"

# Raspberry Pi setup
setup-pi:
	@echo "Installing Raspberry Pi system dependencies..."
	sudo apt update
	sudo apt install -y ffmpeg inotify-tools python3-pip python3-opencv
	@echo "Installing Python dependencies..."
	pip install -e ".[dev]"
	@echo "Setup complete!"

# Python package installation
install:
	pip install -e ".[dev]"

# Run tests
test:
	pytest tests/

test-verbose:
	pytest tests/ -v --tb=short

# Clean build artifacts
clean:
	rm -rf build/ dist/ *.egg-info/ .pytest_cache/ __pycache__/
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true

# Run the system (requires environment variables or CLI args)
run:
	python -m src --verbose
