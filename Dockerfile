# Use an official Python image with build tools
FROM python:3.13-slim

# Install system dependencies for dlib/face_recognition
RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    libopenblas-dev \
    liblapack-dev \
    libx11-dev \
    libgtk-3-dev \
    libboost-all-dev \
    libssl-dev \
    libffi-dev \
    libatlas-base-dev \
    libsm6 \
    libxext6 \
    libxrender-dev \
    && rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy the rest of the backend code
COPY . .

# Expose port (change if your app uses a different port)
EXPOSE 8000

# Start the app (adjust if your entrypoint is different)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
