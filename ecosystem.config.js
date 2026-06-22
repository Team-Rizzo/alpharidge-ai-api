module.exports = {
  apps: [{
    name: 'alpharidge-ai-api',
    script: 'main.py',
    interpreter: '/home/rizzo/miniconda3/envs/alpharidge_ai/bin/python',
    cwd: '/home/rizzo/alpharidge-ai-api',
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '1G',
    env: {
      NODE_ENV: 'production'
    },
    error_file: './logs/pm2-error.log',
    out_file: './logs/pm2-out.log',
    log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
    merge_logs: true,
    time: true
  }]
};


