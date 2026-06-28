FROM node:22-alpine

# Install timezone data for SAST support
RUN apk add --no-cache tzdata

WORKDIR /app

# Copy package files
COPY package*.json ./

# Install dependencies
RUN npm install

# Copy application files
COPY . .

# Expose port
EXPOSE 3000

# Development mode with hot reload
CMD ["npm", "run", "dev"]
