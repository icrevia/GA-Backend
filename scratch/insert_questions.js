const { Client } = require('pg');

const connectionString = 'postgresql://postgres.kijfltbddmesxjjbhdbd:R%40hul007%40%23%40%23%21%21@aws-1-ap-northeast-1.pooler.supabase.com:5432/postgres?sslmode=require';

const client = new Client({
  connectionString,
});

async function run() {
  await client.connect();
  console.log("Connected to Supabase DB!");

  const questions = [
    {
      "question_text": "Which game features a battle royale mode called 'Warzone'?",
      "question_image_url": "https://images.unsplash.com/photo-1542751371-adc38448a05e?auto=format&fit=crop&q=80&w=800",
      "options": ["PUBG", "Free Fire", "Call of Duty", "Fortnite"],
      "correct_option_index": 2,
      "time_limit": 10
    },
    {
      "question_text": "What is the maximum number of players in a standard Free Fire match?",
      "question_image_url": "https://images.unsplash.com/photo-1552824236-07764db0ef91?auto=format&fit=crop&q=80&w=800",
      "options": ["48", "50", "60", "100"],
      "correct_option_index": 1,
      "time_limit": 10
    },
    {
      "question_text": "Which character in Free Fire is a world-famous DJ?",
      "question_image_url": "https://images.unsplash.com/photo-1571266028243-3716f02d2d2e?auto=format&fit=crop&q=80&w=800",
      "options": ["Alok", "Chrono", "K", "Skyler"],
      "correct_option_index": 0,
      "time_limit": 10
    }
  ];

  let count = 0;
  for (const q of questions) {
    await client.query(`
      INSERT INTO quiz_questions 
      (category, question_text, question_image_url, options, correct_option_index, time_limit) 
      VALUES ($1, $2, $3, $4, $5, $6)
    `, [
      "BATTLE_1V1", 
      q.question_text, 
      q.question_image_url, 
      JSON.stringify(q.options), 
      q.correct_option_index, 
      q.time_limit
    ]);
    count++;
  }

  console.log(`Inserted ${count} questions successfully!`);
  await client.end();
}

run().catch(console.error);
