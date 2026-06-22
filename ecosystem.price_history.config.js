module.exports = {
  apps: [{
    name: 'tao-price-history',
    script: 'run_price_history.py',
    interpreter: '/home/rizzo/miniconda3/envs/alpharidge_ai/bin/python',
    cwd: '/home/rizzo/alpharidge-ai-api',
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '500M',
    env: {
      NODE_ENV: 'production'
    },
    error_file: './logs/price_history-error.log',
    out_file: './logs/price_history-out.log',
    log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
    merge_logs: true,
    time: true
  }]
};


