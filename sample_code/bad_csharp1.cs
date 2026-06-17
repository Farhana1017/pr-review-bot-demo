using System;
using System.Data.SqlClient;
using System.Collections.Generic;

namespace Acme.Services
{
    public class UserService
    {
        // BAD: hardcoded connection string with credentials
        private static string connString = "Server=prod-db01;Database=AppDB;User Id=sa;Password=Admin@123!";

        // BAD: no dependency injection — newing up directly
        public UserService() { }

        // BAD: SQL injection via string concatenation
        // BAD: no using block — SqlConnection never disposed
        // BAD: synchronous (no async/await)
        public User Authenticate(string username, string password)
        {
            var conn = new SqlConnection(connString);
            conn.Open();

            string query = "SELECT * FROM Users WHERE Username = '" + username +
                           "' AND Password = '" + password + "'";
            var cmd    = new SqlCommand(query, conn);
            var reader = cmd.ExecuteReader();

            if (reader.Read())
            {
                return new User
                {
                    Id       = (int)reader["Id"],
                    Username = reader["Username"].ToString()
                };
            }
            return null;
        }

        // BAD: SELECT * — fetches all columns even though only two are needed
        // BAD: connection not disposed, no try/catch
        public List<User> GetAllUsers()
        {
            var users = new List<User>();
            var conn  = new SqlConnection(connString);
            conn.Open();

            var cmd    = new SqlCommand("SELECT * FROM Users", conn);
            var reader = cmd.ExecuteReader();

            while (reader.Read())
            {
                users.Add(new User
                {
                    Id       = (int)reader["Id"],
                    Username = reader["Username"].ToString()
                });
            }
            return users;
        }

        // BAD: password stored as plain text
        // BAD: no input validation
        public void ResetPassword(int userId, string newPassword)
        {
            var conn = new SqlConnection(connString);
            conn.Open();
            var cmd = new SqlCommand(
                "UPDATE Users SET Password = '" + newPassword + "' WHERE Id = " + userId,
                conn
            );
            cmd.ExecuteNonQuery();
        }
    }

    public class User
    {
        public int    Id       { get; set; }
        public string Username { get; set; }
    }
}
//test change
