using System;
using System.Data.SqlClient;

namespace SampleApp
{
    // CRITICAL: hardcoded production credentials in source code
    // CRITICAL: SQL injection via string concatenation
    // CRITICAL: SqlConnection never disposed
    // WARNING:  async void instead of async Task
    // STYLE:    no XML docs on public methods

    public class UserService
    {
        // CRITICAL — hardcoded credentials (Security)
        private string connString = "Server=prod-db.company.com;Database=UsersDB;User Id=sa;Password=SuperSecret123!;";

        // CRITICAL — SQL injection (Security)
        public string GetUserByName(string username)
        {
            // SqlConnection opened but never disposed — resource leak (Dispose)
            SqlConnection conn = new SqlConnection(connString);
            conn.Open();

            // Direct string concatenation — trivially injectable
            string query = "SELECT * FROM Users WHERE Username = '" + username + "'";
            SqlCommand cmd = new SqlCommand(query, conn);
            SqlDataReader reader = cmd.ExecuteReader();

            if (reader.Read())
            {
                return reader["Email"].ToString();
            }

            return null;
        }

        // WARNING — async void swallows exceptions (Async)
        public async void SendWelcomeEmail(int userId)
        {
            // Simulated async work — exceptions here are unobservable
            await System.Threading.Tasks.Task.Delay(1000);
            Console.WriteLine($"Email sent to user {userId}");
        }

        // CRITICAL — no null check on input, plus another SQL injection (Bug + Security)
        public void DeleteUser(string userId)
        {
            SqlConnection conn = new SqlConnection(connString);
            conn.Open();

            // userId is never validated and injected directly
            string sql = "DELETE FROM Users WHERE Id = " + userId;
            SqlCommand cmd = new SqlCommand(sql, conn);
            cmd.ExecuteNonQuery();

            // conn never closed or disposed
        }

        // WARNING — catching base Exception and swallowing it (ErrorHandling)
        public int GetUserCount()
        {
            try
            {
                SqlConnection conn = new SqlConnection(connString);
                conn.Open();
                SqlCommand cmd = new SqlCommand("SELECT COUNT(*) FROM Users", conn);
                return (int)cmd.ExecuteScalar();
            }
            catch (Exception)
            {
                // swallowed — caller has no idea this failed
                return 0;
            }
        }
    }
}
